"""Weekly review: what's working, from the database, with suggested changes to targets and settings.

Everything is counted from what the pipeline already tracks: jobs (source, score, status,
app_status from company emails), applications, and outreach messages and replies. Suggestions are
plain rules over those numbers and are only made once there's enough data to mean something.
Nothing is changed automatically; config.toml stays yours.
"""
import html
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from watcher.db import JobStore

POSITIVE = ("assessment", "interview", "offer")
BANDS = [(90, 101, "90+"), (80, 90, "80-89"), (70, 80, "70-79"), (0, 70, "under 70")]
RANK_NAMES = {"job": {3: "hiring managers", 2: "engineers", 1: "recruiters"},
              "network": {3: "founders", 2: "recruiters", 1: "others"}}
MIN_SAMPLE = 8      # don't draw conclusions from fewer applications or emails than this


def _rate(n: int, d: int) -> str:
    return f"{n}/{d} ({round(100 * n / d)}%)" if d else "0/0"


def compute(store: JobStore, since: datetime, until: datetime) -> dict:
    s, u = since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")
    q = store.conn.execute
    week = {
        "found": q("SELECT COUNT(*) FROM jobs WHERE first_seen >= ? AND first_seen < ?", (s, u)).fetchone()[0],
        "matched": q("SELECT COUNT(*) FROM jobs WHERE matched=1 AND is_new=1 AND first_seen >= ? AND first_seen < ?",
                     (s, u)).fetchone()[0],
        "tailored": q("SELECT COUNT(*) FROM tailored WHERE created_at >= ? AND created_at < ?", (s, u)).fetchone()[0],
        "submitted": q("SELECT COUNT(*) FROM applications WHERE submitted_at >= ? AND submitted_at < ?",
                       (s, u)).fetchone()[0],
        "needs_you": q("SELECT COUNT(*) FROM applications WHERE outcome='needs_you' AND last_attempt_at >= ? "
                       "AND last_attempt_at < ?", (s, u)).fetchone()[0],
        "failed": q("SELECT COUNT(*) FROM applications WHERE outcome='failed' AND last_attempt_at >= ? "
                    "AND last_attempt_at < ?", (s, u)).fetchone()[0],
        "emails_sent": q("SELECT COUNT(*) FROM messages WHERE channel='email' AND sent_at >= ? AND sent_at < ?",
                         (s, u)).fetchone()[0],
        "replies": q("SELECT COUNT(*) FROM messages WHERE replied_at >= ? AND replied_at < ?", (s, u)).fetchone()[0],
        "responses": q("SELECT category, COUNT(*) AS n FROM inbox WHERE kind='application' AND received_at >= ? "
                       "AND received_at < ? GROUP BY category", (s, u)).fetchall(),
    }
    week["responses"] = {r["category"]: r["n"] for r in week["responses"]}

    # All-time: which sources, score bands, roles and contacts turn into responses
    applied = q("SELECT ats, title, company, score, app_status FROM jobs WHERE status='submitted' "
                "OR app_status IS NOT NULL").fetchall()

    def group(key):
        out = defaultdict(lambda: [0, 0, 0])   # applied, positive, rejected
        for r in applied:
            k = key(r)
            if k is None:
                continue
            out[k][0] += 1
            out[k][1] += r["app_status"] in POSITIVE
            out[k][2] += r["app_status"] == "rejected"
        return {k: {"applied": v[0], "positive": v[1], "rejected": v[2]} for k, v in
                sorted(out.items(), key=lambda kv: -kv[1][0])}

    def band(r):
        if r["score"] is None or r["score"] < 0:
            return None
        return next(name for lo, hi, name in BANDS if lo <= r["score"] < hi)

    def role(r):
        t = r["title"].lower()
        for word in ("machine learning", "ml", "data", "platform", "infrastructure", "backend", "frontend",
                     "full stack", "fullstack", "new grad", "intern"):
            if word in t:
                return word
        return "other"

    outreach = defaultdict(lambda: [0, 0])
    for r in q("SELECT c.kind, c.rank, m.status, m.is_followup FROM messages m JOIN contacts c ON c.id = m.contact_id "
               "WHERE m.channel='email' AND m.sent_at IS NOT NULL").fetchall():
        name = f"{r['kind']}: {RANK_NAMES.get(r['kind'], {}).get(r['rank'] or 0, 'others')}"
        outreach[name][0] += 1
        outreach[name][1] += r["status"] == "replied"
    linkedin = q("SELECT COUNT(*) AS sent, SUM(status='replied') AS replied FROM messages "
                 "WHERE channel='linkedin' AND status IN ('sent', 'replied')").fetchone()
    reasons = defaultdict(int)
    for r in q("SELECT reasons FROM applications WHERE outcome IN ('needs_you', 'failed')").fetchall():
        for reason in json.loads(r["reasons"] or "[]"):
            reasons[reason.split(":")[0].strip("“”\" ")[:60]] += 1
    boards = q("SELECT ats, company, last_error FROM boards WHERE last_error IS NOT NULL").fetchall()

    return {
        "since": s[:10], "until": u[:10], "week": week,
        "by_source": group(lambda r: r["ats"]),
        "by_band": group(band),
        "by_role": group(role),
        "outreach": {k: {"sent": v[0], "replied": v[1]} for k, v in sorted(outreach.items())},
        "linkedin": {"sent": linkedin["sent"] or 0, "replied": linkedin["replied"] or 0},
        "stuck_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:6]),
        "broken_boards": [f"{b['ats']}/{b['company']}: {b['last_error'][:80]}" for b in boards],
        "total_applied": len(applied),
        "total_positive": sum(r["app_status"] in POSITIVE for r in applied),
    }


def suggestions(stats: dict, cfg: dict) -> list[str]:
    out = []
    threshold = cfg.get("pipeline", {}).get("score_threshold", 70)
    bands = stats["by_band"]
    low, high = bands.get("70-79"), bands.get("90+")
    if low and high and low["applied"] >= MIN_SAMPLE and high["applied"] >= MIN_SAMPLE:
        lo_rate, hi_rate = low["positive"] / low["applied"], high["positive"] / high["applied"]
        if lo_rate == 0 and hi_rate > 0 and threshold < 80:
            out.append(f"Jobs scored 70-79 got no responses in {low['applied']} applications while 90+ did; "
                       f"consider score_threshold = 80 to spend your applications on stronger fits.")
        elif lo_rate >= hi_rate > 0:
            out.append("Jobs scored 70-79 respond as well as 90+; the score threshold could go down to 65 for more volume.")
    for src, v in stats["by_source"].items():
        if v["applied"] >= MIN_SAMPLE and v["positive"] == 0:
            out.append(f"No responses yet from {v['applied']} applications found on {src}; "
                       f"consider fewer searches there or tighter titles.")
    for role, v in stats["by_role"].items():
        if v["applied"] >= MIN_SAMPLE and v["positive"] / v["applied"] >= 0.2:
            out.append(f"'{role}' roles respond well ({_rate(v['positive'], v['applied'])}); add more of them to "
                       f"title_include and discovery searches.")
    for name, v in stats["outreach"].items():
        if v["sent"] >= MIN_SAMPLE and v["replied"] == 0:
            out.append(f"Emails to {name} have no replies in {v['sent']} sends; try a shorter ask or other contacts.")
    week = stats["week"]
    if week["needs_you"] + week["failed"] > week["submitted"] and week["needs_you"] + week["failed"] >= 3:
        top = next(iter(stats["stuck_reasons"]), "")
        out.append(f"More applications came back to you than went through this week; the most common reason "
                   f"was “{top}”. Filling that in answers.yaml would unblock them.")
    if stats["broken_boards"]:
        out.append(f"{len(stats['broken_boards'])} company board(s) are failing; check their slugs in config.toml.")
    if week["matched"] == 0:
        out.append("No new matching jobs this week; widen title_include, locations, or add companies and searches.")
    return out


def format_telegram(stats: dict, tips: list[str]) -> str:
    w, e = stats["week"], html.escape
    resp = w["responses"]
    lines = [f"📊 <b>Weekly review</b> ({stats['since']} to {stats['until']})",
             f"Found {w['found']} jobs, {w['matched']} new matches, tailored {w['tailored']}.",
             f"Applied to {w['submitted']}; {w['needs_you']} came back to you, {w['failed']} failed.",
             f"Company emails: {resp.get('interview_request', 0)} interviews, {resp.get('assessment', 0)} assessments, "
             f"{resp.get('offer', 0)} offers, {resp.get('rejection', 0)} rejections.",
             f"Outreach: {w['emails_sent']} emails sent, {w['replies']} replies.",
             f"All time: {_rate(stats['total_positive'], stats['total_applied'])} applications got a response."]
    if tips:
        lines.append("\n<b>Suggestions</b>")
        lines += [f"• {e(t)}" for t in tips]
    return "\n".join(lines)


def week_window(at: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local = at.astimezone(tz)
    end = local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (end - timedelta(days=7)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def due(store: JobStore, cfg: dict, at: datetime | None = None) -> bool:
    r = cfg.get("review")
    if not r or not r.get("enabled", True):     # on via the [review] section in config.toml
        return False
    tz = ZoneInfo(r.get("timezone", cfg.get("networking", {}).get("timezone", "America/Edmonton")))
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    week = f"{local.isocalendar().year}-W{local.isocalendar().week}"
    return (local.weekday() == r.get("weekday", 6) and local.hour >= r.get("hour", 18)
            and store.get_state("review_last_week") != week)


def mark_sent(store: JobStore, cfg: dict, at: datetime | None = None) -> None:
    tz = ZoneInfo(cfg.get("review", {}).get("timezone", cfg.get("networking", {}).get("timezone", "America/Edmonton")))
    local = (at or datetime.now(timezone.utc)).astimezone(tz)
    store.set_state("review_last_week", f"{local.isocalendar().year}-W{local.isocalendar().week}")
