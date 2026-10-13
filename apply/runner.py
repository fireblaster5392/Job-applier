"""Apply to one approved job: fill the form, and submit only when nothing needs the user.

Outcomes:
  submitted     form sent and the page confirmed it
  needs_review  Claude wrote free-text answers; the user reviews them and taps Submit
  needs_you     a CAPTCHA, or a required question nobody can answer (legal, salary, unknown);
                the user finishes it themselves with the attached PDFs
  failed        something broke (page didn't load, submit didn't confirm)

Greenhouse, Lever and Ashby forms use the generic label-driven filler. Anything else (Workday,
iCIMS, company portals, or a form the filler can't read) goes to the browser agent in agent.py
when [apply] agent is on, and comes back to you otherwise.
"""
import json
import logging
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from profile_mcp.profile import Profile
from tailor.schema import TailoredApplication
from watcher.db import JobStore, now, row_to_job
from watcher.sources import job_description

from . import browser as br
from .agent import run_agent
from .fields import CLAUDE_OK, Field, answer_known, classify
from .llm import answer_questions
from .pdf import render_pdfs

log = logging.getLogger(__name__)
FREE_TEXT = {"textarea", "text"}
# Portals the generic filler shouldn't try (accounts, multi-page wizards): straight to the agent.
AGENT_HOSTS = ("myworkdayjobs.com", "myworkdaysite.com", "workday.com", "icims.com", "taleo.net",
               "smartrecruiters.com", "successfactors.com", "successfactors.eu", "jobvite.com",
               "oraclecloud.com", "brassring.com", "indeed.com", "linkedin.com", "glassdoor.com")


def agent_host(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in AGENT_HOSTS)


def _key(f: Field) -> str:
    return f"{f.type}|{f.label.lower()}"


def apply_job(store: JobStore, rowid: int, profile: Profile, *, model: str, out_root: Path,
              claude=None, approved_answers: dict[str, str] | None = None, dry_run: bool = False,
              playwright_factory=sync_playwright, http_client=None, agent_model: str | None = None,
              agent_fn=run_agent) -> dict:
    """agent_model: model for the browser-agent fallback; None turns the fallback off."""
    row = store.get_job(rowid)
    rec = store.get_tailored(rowid)
    if row is None or rec is None:
        return _save(store, rowid, "failed", [], ["This job hasn't been tailored yet."], None)
    job = row_to_job(row)
    app = TailoredApplication.model_validate(rec["data"])
    out_dir = out_root / str(rowid)
    screenshot = str(out_dir / "form.png")

    with playwright_factory() as pw:
        b = br.launch(pw)
        try:
            files = render_pdfs(b, profile, app, job.company, out_dir)
            page = b.new_page(viewport={"width": 1280, "height": 1600})
            br.open_form(page, job.apply_url or job.url)
            if br.has_captcha(page):
                page.screenshot(path=screenshot, full_page=True)
                return _save(store, rowid, "needs_you", [], ["The form has a CAPTCHA."], screenshot, files)

            fields = [] if agent_host(page.url) else [classify(f) for f in br.extract_fields(page)]
            if not fields and agent_model:
                approved = None if approved_answers is None else [
                    {"label": k.split("|", 1)[-1], "answer": v} for k, v in approved_answers.items()]
                if dry_run:
                    approved = None     # a dry run never submits
                r = agent_fn(page, job={"title": job.title, "company": job.company}, profile=profile, app=app,
                             files=files, model=agent_model, client=claude, approved=approved)
                page.screenshot(path=screenshot, full_page=True)
                return _save(store, rowid, r["outcome"], r["answers"], r["reasons"], screenshot, files)
            if not fields and agent_host(page.url):
                page.screenshot(path=screenshot, full_page=True)
                return _save(store, rowid, "needs_you", [], [
                    f"{urlparse(page.url).hostname} needs the browser agent, which is off ([apply] agent)."],
                    screenshot, files)
            if not fields:
                page.screenshot(path=screenshot, full_page=True)
                return _save(store, rowid, "failed", [], ["Couldn't find the application form."], screenshot, files)

            filled, reasons, ask_claude = [], [], []
            for f in fields:
                if approved_answers and _key(f) in approved_answers:
                    filled.append((f, approved_answers[_key(f)], "you"))
                    continue
                if f.kind == "cover_letter" and f.type != "file":
                    filled.append((f, app.cover_letter, "tailored"))
                    continue
                if f.kind == "company":
                    current = next(iter(profile.data.get("experience", [])), {}).get("company", "")
                    if current:
                        filled.append((f, current, "profile"))
                        continue
                value, why = answer_known(f, profile.basics, profile.answers, job.location, files)
                if value is not None:
                    if value != "":
                        filled.append((f, value, "profile"))
                elif f.kind in CLAUDE_OK:
                    ask_claude.append(f)
                elif f.required:          # legal, salary, contact details: only your saved answers count
                    reasons.append(f"“{f.label}”: {why}")

            if ask_claude:
                desc = rec.get("description") or job_description(job, http_client)
                got = answer_questions(ask_claude, {"title": job.title, "company": job.company}, desc, profile,
                                       model=model, client=claude)
                for f in ask_claude:
                    if got.get(f.id):
                        filled.append((f, got[f.id], "claude"))
                    elif f.required:
                        reasons.append(f"“{f.label}”: not something your profile answers")

            for f, value, _ in filled:
                try:
                    br.fill(page, f, value)
                except Exception as exc:  # noqa: BLE001
                    if f.required:
                        reasons.append(f"“{f.label}”: couldn't fill it ({type(exc).__name__})")
            page.screenshot(path=screenshot, full_page=True)
            answers = [{"label": f.label, "type": f.type, "key": _key(f), "answer": v, "source": src}
                       for f, v, src in filled if f.type != "file"]

            if reasons:
                return _save(store, rowid, "needs_you", answers, reasons, screenshot, files)
            wrote = [a for a in answers if a["source"] == "claude" and a["type"] in FREE_TEXT]
            if wrote and not approved_answers:
                return _save(store, rowid, "needs_review", answers,
                             [f"Claude wrote {len(wrote)} answer(s); check them before it submits."], screenshot, files)
            if dry_run:
                return _save(store, rowid, "needs_review", answers, ["Dry run: filled but not submitted."], screenshot, files)

            ok = br.submit(page)
            page.screenshot(path=screenshot, full_page=True)
            if not ok:
                return _save(store, rowid, "failed", answers,
                             ["Clicked Submit but the page didn't confirm it. Check the screenshot; it may need a field fixed."],
                             screenshot, files)
            return _save(store, rowid, "submitted", answers, [], screenshot, files)
        except Exception as exc:  # noqa: BLE001
            log.exception("apply failed for job %s", rowid)
            return _save(store, rowid, "failed", [], [f"{type(exc).__name__}: {exc}"], None)
        finally:
            b.close()


STATUS = {"submitted": "submitted", "needs_review": "needs_review", "needs_you": "needs_you", "failed": "apply_failed"}


def _save(store: JobStore, rowid: int, outcome: str, answers: list, reasons: list, screenshot: str | None,
          files: dict | None = None) -> dict:
    ts = now()
    store.conn.execute(
        """INSERT INTO applications (job_rowid, outcome, answers, reasons, screenshot, attempts, last_attempt_at, submitted_at)
           VALUES (?, ?, ?, ?, ?, 1, ?, ?)
           ON CONFLICT (job_rowid) DO UPDATE SET outcome=excluded.outcome, answers=excluded.answers,
             reasons=excluded.reasons, screenshot=COALESCE(excluded.screenshot, applications.screenshot),
             attempts=applications.attempts + 1, last_attempt_at=excluded.last_attempt_at,
             submitted_at=COALESCE(excluded.submitted_at, applications.submitted_at)""",
        (rowid, outcome, json.dumps(answers), json.dumps(reasons), screenshot, ts, ts if outcome == "submitted" else None))
    store.conn.execute("UPDATE jobs SET status=?, app_status=CASE WHEN ?='submitted' THEN 'applied' ELSE app_status END"
                       " WHERE rowid=?", (STATUS[outcome], outcome, rowid))
    store.conn.commit()
    return {"outcome": outcome, "answers": answers, "reasons": reasons, "screenshot": screenshot, "files": files or {}}


def get_application(store: JobStore, rowid: int) -> dict | None:
    r = store.conn.execute("SELECT * FROM applications WHERE job_rowid=?", (rowid,)).fetchone()
    if not r:
        return None
    return {**dict(r), "answers": json.loads(r["answers"] or "[]"), "reasons": json.loads(r["reasons"] or "[]")}
