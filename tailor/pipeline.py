"""Tailor one stored job end to end: fetch description, call Claude, enforce guardrails, save."""
import logging

from profile_mcp.profile import Profile
from watcher.db import JobStore, row_to_job
from watcher.sources import job_description

from .claude import DEFAULT_MODEL, TailorError, tailor
from .guardrails import enforce

log = logging.getLogger(__name__)


def tailor_job(store: JobStore, rowid: int, profile: Profile, *, model: str = DEFAULT_MODEL,
               client=None, http_client=None, description: str | None = None) -> dict:
    """Returns the saved tailoring record. Sets jobs.status to tailored, or failed with last_error."""
    row = store.get_job(rowid)
    if row is None:
        raise KeyError(f"no job with id {rowid}")
    job = row_to_job(row)
    store.set_status(rowid, "tailoring")
    try:
        description = description or job_description(job, http_client)
        if not description:
            raise TailorError("Couldn't get the job description.")
        app, usage = tailor({"title": job.title, "company": job.company, "location": job.location},
                            description, profile, model=model, client=client)
        warnings = enforce(app, profile)
    except Exception as exc:  # noqa: BLE001 - any failure is shown on the job, not raised into the bot loop
        log.exception("tailoring failed for job %s", rowid)
        store.set_status(rowid, "failed", str(exc))
        raise
    store.save_tailored(rowid, model=usage["model"], description=description,
                        data=app.model_dump(), warnings=warnings, usage=usage)
    store.set_status(rowid, "tailored")
    return store.get_tailored(rowid)
