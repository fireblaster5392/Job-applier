"""Approval queue + dashboard.

    DB_PATH=data/jobs.db uvicorn --factory web.app:main_app --host 127.0.0.1 --port 8000

Keep it private (Tailscale or Cloudflare Access); it has no login of its own.
"""
import functools
import os
import threading

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from apply.runner import get_application
from apply.status import mark_submitted_by_user
from outreach.mailer import SmtpMailer
from outreach.store import (get_message, mark_linkedin_sent, mark_replied, network_contacts, outreach_for_job,
                            send_email_message, skip_message)
from profile_mcp.profile import Profile
from tailor.claude import DEFAULT_MODEL
from tailor.pipeline import tailor_job
from tailor.provider import load_provider
from tailor.render import cover_letter_html, resume_html
from tailor.schema import TailoredApplication
from watcher.db import JobStore
from watcher.main import load_config

from .templates import env

STATUSES = ["tailored", "needs_review", "needs_you", "approved", "submitted", "below_threshold", "discovered",
            "apply_failed", "rejected", "skipped", "failed"]
SETTABLE = {"approved", "rejected", "skipped", "discovered"}


def create_app(db_path: str, profile_loader=Profile.load, tailor=tailor_job,
               model: str = DEFAULT_MODEL, mailer=None, max_emails_per_day: int = 10) -> FastAPI:
    app = FastAPI(title="Job pipeline")
    store = JobStore(db_path)
    lock = threading.Lock()  # request handlers share one sqlite connection

    def job_or_404(rowid: int):
        with lock:
            job = store.get_job(rowid)
        if job is None:
            raise HTTPException(404, "Job not found")
        return job

    def tailored_or_404(rowid: int):
        with lock:
            rec = store.get_tailored(rowid)
        if rec is None:
            raise HTTPException(404, "Not tailored yet")
        return TailoredApplication.model_validate(rec["data"])

    @app.get("/", response_class=HTMLResponse)
    def index(status: str | None = None):
        with lock:
            counts = store.status_counts()
            jobs = store.list_jobs(status=status or None)
        return env.get_template("index").render(
            jobs=jobs, counts=counts, total=sum(counts.values()), statuses=STATUSES, status=status,
            subtitle=f"{counts.get('tailored', 0)} waiting for review")

    @app.get("/jobs/{rowid}", response_class=HTMLResponse)
    def job_page(rowid: int):
        job = job_or_404(rowid)
        with lock:
            t = store.get_tailored(rowid)
            outreach = outreach_for_job(store, rowid)
            app_emails = store.conn.execute(
                "SELECT * FROM inbox WHERE job_rowid=? AND kind='application' ORDER BY received_at", (rowid,)).fetchall()
            application = get_application(store, rowid)
        return env.get_template("job").render(job=job, t=t, outreach=outreach, app_emails=app_emails,
                                              application=application)

    @app.get("/jobs/{rowid}/screenshot")
    def screenshot(rowid: int):
        with lock:
            a = get_application(store, rowid)
        if not a or not a["screenshot"] or not os.path.exists(a["screenshot"]):
            raise HTTPException(404, "No screenshot")
        return FileResponse(a["screenshot"], media_type="image/png")

    @app.post("/jobs/{rowid}/apply/{action}")
    def apply_action(rowid: int, action: str):
        job = job_or_404(rowid)
        with lock:
            if action == "submit" and job["status"] == "needs_review":
                store.set_status(rowid, "submit_requested")
            elif action == "retry" and job["status"] in ("apply_failed", "needs_you", "needs_review"):
                store.set_status(rowid, "approved")
            elif action == "done":
                mark_submitted_by_user(store, rowid)
            else:
                raise HTTPException(400, f"can't {action} a job that is {job['status']}")
        return RedirectResponse(f"/jobs/{rowid}", status_code=303)

    @app.get("/network", response_class=HTMLResponse)
    def network():
        with lock:
            people = network_contacts(store)
        counts = {}
        for p in people:
            counts[p["state"]] = counts.get(p["state"], 0) + 1
        return env.get_template("network").render(people=people, counts=counts,
                                                  subtitle=f"{counts.get('replied', 0)} replied")

    def back(m) -> RedirectResponse:
        return RedirectResponse(f"/jobs/{m['job_rowid']}#outreach" if m["job_rowid"] else "/network", status_code=303)

    @app.post("/messages/{msg_id}/linkedin-sent")
    def linkedin_sent(msg_id: int):
        with lock:
            m = get_message(store, msg_id)
            if m is None:
                raise HTTPException(404, "Message not found")
            mark_linkedin_sent(store, msg_id)
        return back(m)

    @app.post("/messages/{msg_id}/replied")
    def replied(msg_id: int):
        with lock:
            m = get_message(store, msg_id)
            if m is None:
                raise HTTPException(404, "Message not found")
            mark_replied(store, msg_id)
        return back(m)

    @app.post("/messages/{msg_id}/send")
    def send_message(msg_id: int):
        with lock:
            m = get_message(store, msg_id)
            if m is None:
                raise HTTPException(404, "Message not found")
            send_email_message(store, msg_id, mailer or SmtpMailer(), max_emails_per_day)
        return back(m)

    @app.post("/messages/{msg_id}/skip")
    def skip(msg_id: int):
        with lock:
            m = get_message(store, msg_id)
            if m is None:
                raise HTTPException(404, "Message not found")
            skip_message(store, msg_id)
        return back(m)

    @app.get("/jobs/{rowid}/edit", response_class=HTMLResponse)
    def edit_page(rowid: int):
        job = job_or_404(rowid)
        with lock:
            t = store.get_tailored(rowid)
        if t is None:
            return RedirectResponse(f"/jobs/{rowid}", status_code=303)
        return env.get_template("edit").render(job=job, t=t)

    @app.post("/jobs/{rowid}/edit")
    async def save_edit(rowid: int, request: Request):
        job_or_404(rowid)
        form = await request.form()
        with lock:
            rec = store.get_tailored(rowid)
            if rec is None:
                raise HTTPException(404, "Not tailored yet")
            data = rec["data"]
            data["summary"] = str(form.get("summary", data["summary"])).strip()
            data["cover_letter"] = str(form.get("cover_letter", data["cover_letter"])).replace("\r\n", "\n").strip()
            data["skills"] = [s.strip() for s in str(form.get("skills", "")).split(",") if s.strip()]
            bullets = []
            for b in data["bullets"]:
                text = str(form.get(f"bullet_{b['id']}", b["text"])).strip()
                if text:                      # clear a bullet's text to remove it
                    bullets.append({"id": b["id"], "text": text})
            data["bullets"] = bullets
            # Your own edits are trusted, so guardrails don't re-run on them.
            store.update_tailored_data(rowid, data, "You edited this draft.")
        return RedirectResponse(f"/jobs/{rowid}", status_code=303)

    @app.get("/jobs/{rowid}/resume", response_class=HTMLResponse)
    def resume(rowid: int):
        return resume_html(profile_loader(), tailored_or_404(rowid))

    @app.get("/jobs/{rowid}/cover-letter", response_class=HTMLResponse)
    def cover_letter(rowid: int):
        job = job_or_404(rowid)
        return cover_letter_html(profile_loader(), tailored_or_404(rowid), job["company"])

    def run_tailor(rowid: int):
        try:
            # Own connection: tailoring takes a while and mustn't block page loads.
            tailor(JobStore(db_path), rowid, profile_loader(), model=model)
        except Exception:  # noqa: BLE001 - the error is stored on the job and shown on its page
            pass

    @app.post("/jobs/{rowid}/tailor")
    def start_tailor(rowid: int, background: BackgroundTasks):
        job = job_or_404(rowid)
        if job["status"] != "tailoring":
            with lock:
                store.set_status(rowid, "tailoring")
            background.add_task(run_tailor, rowid)
        return RedirectResponse(f"/jobs/{rowid}", status_code=303)

    @app.post("/jobs/{rowid}/status")
    def set_status(rowid: int, status: str = Form(...)):
        job_or_404(rowid)
        if status not in SETTABLE:
            raise HTTPException(400, f"status must be one of {sorted(SETTABLE)}")
        with lock:
            store.set_status(rowid, status)
        return RedirectResponse(f"/jobs/{rowid}", status_code=303)

    return app


def main_app() -> FastAPI:
    cfg_path = os.environ.get("CONFIG_PATH", "config.toml")
    cfg = load_config(cfg_path) if os.path.exists(cfg_path) else {}
    provider = load_provider(cfg)
    return create_app(os.environ.get("DB_PATH", "data/jobs.db"),
                      profile_loader=provider.profile, tailor=functools.partial(tailor_job, provider=provider),
                      model=cfg.get("tailor", {}).get("model", DEFAULT_MODEL),
                      max_emails_per_day=cfg.get("outreach", {}).get("max_emails_per_day", 10))
