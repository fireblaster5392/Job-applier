import json

import anthropic
import httpx2

from watcher.db import JobStore
from watcher.models import Job

GOOD_OUTPUT = {
    "fit_score": 82,
    "fit_reasons": ["Strong Python backend work", "Data pipeline experience"],
    "gaps": ["No Kubernetes"],
    "summary": "Backend engineer who builds Python services and data pipelines.",
    "bullets": [
        {"id": "exco-api", "text": "Built a Python/FastAPI billing service handling 2M requests a day."},
        {"id": "exco-pipeline", "text": "Cut nightly ETL runtime from 3 hours to 5 minutes."},   # invented number
        {"id": "made-up", "text": "Led a team of 40 engineers."},                               # unknown id
        {"id": "proj-watcher", "text": "Async Python service polling 40 job boards every 60 seconds."},
    ],
    "skills": ["Python", "FastAPI", "Kubernetes"],                                               # Kubernetes not in profile
    "cover_letter": "First paragraph.\n\nSecond paragraph.\n\nThird paragraph.",
}


def fake_claude(output=GOOD_OUTPUT, stop_reason="end_turn", status=200, calls=None):
    def handler(request):
        if calls is not None:
            calls.append(json.loads(request.content))
        if status != 200:
            return httpx2.Response(status, json={"type": "error", "error": {"type": "api_error", "message": "boom"}})
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "stop_reason": stop_reason, "stop_sequence": None,
            "content": [{"type": "text", "text": json.dumps(output)}],
            "usage": {"input_tokens": 1200, "output_tokens": 600, "cache_read_input_tokens": 0},
        })
    return anthropic.Anthropic(api_key="test", max_retries=0,
                               http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))


def store_with_job(path=":memory:", ats="lever", raw=None) -> tuple[JobStore, int]:
    store = JobStore(path)
    job = Job(ats, "acme", "j1", "Backend Engineer", "Remote - US", "https://jobs.lever.co/acme/j1",
              apply_url="https://jobs.lever.co/acme/j1/apply", remote=True,
              raw=raw if raw is not None else {"descriptionPlain": "We need Python, FastAPI and Spark."})
    store.sync_board(ats, "acme", [], lambda j: True)          # seed
    store.sync_board(ats, "acme", [job], lambda j: True)       # job arrives as new
    rowid = store.list_jobs()[0]["rowid"]
    return store, rowid
