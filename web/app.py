"""Approval queue + dashboard.

    DB_PATH=data/jobs.db uvicorn --factory web.app:main_app --host 127.0.0.1 --port 8000

Keep it private (Tailscale or Cloudflare Access); it has no login of its own.
"""
import os
import threading

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from profile_mcp.profile import Profile
from tailor.claude import DEFAULT_MODEL
from tailor.pipeline import tailor_job
from tailor.render import cover_letter_html, resume_html
from tailor.schema import TailoredApplication
from watcher.db import JobStore
from watcher.main import load_config

from .templates import env

STATUSES = ["discovered", "tailoring", "tailored", "approved", "rejected", "skipped", "failed"]
SETTABLE = {"approved", "rejected", "skipped", "discovered"}


def create_app(db_path: str, profile_loader=Profile.load, tailor=tailor_job,
               model: str = DEFAULT_MODEL) -> FastAPI:
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
        return env.get_template("job").render(job=job, t=t)

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
    return create_app(os.environ.get("DB_PATH", "data/jobs.db"),
                      model=cfg.get("tailor", {}).get("model", DEFAULT_MODEL))
