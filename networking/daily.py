"""The daily networking batch and follow-up reminders."""
import html
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from outreach.finder import find_contacts
from outreach.store import save_outreach
from outreach.telegram import email_buttons, send_contact_drafts
from watcher.db import JobStore, now

from .draft import draft_network_messages

log = logging.getLogger(__name__)


def target_companies(cfg: dict) -> list[str]:
    """[networking] companies, or every company the watcher follows."""
    n = cfg.get("networking", {})
    if n.get("companies"):
        return list(n["companies"])
    seen, out = set(), []
    for ats in ("ashby", "lever", "greenhouse"):   # Ashby/Lever boards skew to startups, so they go first
        for slug in cfg.get("companies", {}).get(ats, []):
            if slug not in seen:
                seen.add(slug)
                out.append(slug)
    return out


def due(store: JobStore, cfg: dict, at: datetime | None = None) -> bool:
    n = cfg.get("networking", {})
    tz = ZoneInfo(n.get("timezone", "America/Edmonton"))
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    return local.hour >= n.get("daily_hour", 9) and store.get_state("network_last_day") != local.date().isoformat()


def pick_companies(store: JobStore, companies: list[str], cooldown_days: int) -> list[str]:
    """Companies with nobody contacted in the last cooldown_days, least recently contacted first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=cooldown_days)).isoformat()
    last = {r["company"]: r["last"] for r in store.conn.execute(
        "SELECT company, MAX(created_at) AS last FROM contacts WHERE company IS NOT NULL GROUP BY company")}
    eligible = [c for c in companies if not last.get(c) or last[c] < cutoff]
    return sorted(eligible, key=lambda c: last.get(c) or "")


def posted_roles(store: JobStore, company: str) -> list[str]:
    return [r["title"] for r in store.conn.execute(
        "SELECT title FROM jobs WHERE company=? AND closed_at IS NULL ORDER BY first_seen DESC LIMIT 5", (company,))]


async def run_daily(store: JobStore, notifier, cfg: dict, profile, *, claude=None, finder=find_contacts,
                    to_thread=None) -> int:
    """Find and draft today's batch, send it to Telegram. Returns how many people were drafted."""
    import asyncio
    to_thread = to_thread or asyncio.to_thread
    n = cfg.get("networking", {})
    per_day = n.get("per_day", 5)
    model = n.get("model", cfg.get("outreach", {}).get("model", "claude-opus-5-5"))
    people = []
    for company in pick_companies(store, target_companies(cfg), n.get("cooldown_days", 90)):
        if len(people) >= per_day:
            break
        try:
            found = await to_thread(finder, company, "", limit=1, mode="network")
        except Exception as exc:  # noqa: BLE001
            log.warning("networking search failed for %s: %s", company, exc)
            continue
        if found:
            people.append((found[0], posted_roles(store, company)))

    if not people:
        await notifier.send_text("🌱 No new networking contacts found today. Check BRAVE_API_KEY / APOLLO_API_KEY "
                                 "and the [networking] companies list.")
        return 0
    drafts = await to_thread(draft_network_messages, people, profile, model=model, client=claude)
    saved = save_outreach(store, None, [c for c, _ in people], drafts, kind="network")
    await notifier.send_text(f"🌱 Today's networking: {len(saved)} people. Emails send only when you tap; "
                             "LinkedIn notes are for you to paste.")
    await send_contact_drafts(notifier, saved, drafts)
    return len(saved)


FOLLOWUP_EMAIL = """Hi {first},

Just bumping this in case it got buried. I'd still love 15 minutes to hear about {company} whenever \
suits you, and I'm happy to work around your schedule.

Thanks,
{me}"""

FOLLOWUP_NOTE = "Hi {first}, following up on my note: I'd still love a quick 15-minute chat about {company} if you have time."


async def followups(store: JobStore, notifier, cfg: dict, profile) -> int:
    """One follow-up per contact, N days after a message got no reply."""
    days = cfg.get("networking", {}).get("followup_days", 5)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = store.conn.execute(
        "SELECT m.*, c.name, c.email, c.company, c.linkedin_url, c.title FROM messages m JOIN contacts c ON c.id = m.contact_id"
        " WHERE m.status='sent' AND m.is_followup=0 AND m.sent_at < ?"
        " AND NOT EXISTS (SELECT 1 FROM messages f WHERE f.contact_id = m.contact_id AND f.is_followup=1)"
        " AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.contact_id = m.contact_id AND r.status='replied')",
        (cutoff,)).fetchall()
    e = html.escape
    me = (profile.basics.get("name") or "").split(" ")[0]
    count = 0
    for m in rows:
        first = m["name"].split(" ")[0]
        company = m["company"] or "your team"
        head = f"⏰ No reply from <b>{e(m['name'])}</b> ({e(company)}) after {days} days."
        if m["channel"] == "email":
            body = FOLLOWUP_EMAIL.format(first=first, company=company, me=me)
            fid = store.conn.execute(
                "INSERT INTO messages (contact_id, channel, subject, body, is_followup, created_at)"
                " VALUES (?, 'email', ?, ?, 1, ?)", (m["contact_id"], m["subject"], body, now())).lastrowid
            store.conn.commit()
            await notifier.send_text(f"{head} Follow-up, sent in the same thread:\n\n{e(body)}", email_buttons(fid))
        else:
            note = FOLLOWUP_NOTE.format(first=first, company=company)
            fid = store.conn.execute(
                "INSERT INTO messages (contact_id, channel, body, is_followup, created_at) VALUES (?, 'linkedin', ?, 1, ?)",
                (m["contact_id"], note, now())).lastrowid
            store.conn.commit()
            buttons = [[{"text": "Open LinkedIn profile", "url": m["linkedin_url"]},
                        {"text": "I sent it", "callback_data": f"lisent:{fid}"}]] if m["linkedin_url"] else None
            await notifier.send_text(f"{head} Follow-up note to paste:\n<code>{e(note)}</code>", buttons)
        count += 1
    return count
