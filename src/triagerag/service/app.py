"""Jira webhook receiver: verify the signature, enqueue a job, return immediately.
issue created → triage_ticket (once per issue); issue resolved → index_ticket.
The slow parts run in workers (triagerag.jobs.worker)."""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool

from triagerag.config import settings
from triagerag.jobs.queue import enqueue
from triagerag.shared.redact import redact

app = FastAPI(title="triagerag")

INLINE_PARENTS = {"paragraph", "heading", "codeBlock"}


def verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    if not secret:
        raise HTTPException(500, "webhook secret not configured")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # compare_digest takes the same time whether the first or last character differs,
    # so an attacker can't recover the signature byte by byte from response timings
    if not signature or not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "bad signature")


def adf_text(node, parent: str = "doc") -> str:
    """Jira Cloud sends descriptions as Atlassian Document Format (JSON). Flatten to plain text."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    sep = "" if kind in INLINE_PARENTS else "\n"
    return sep.join(adf_text(c, kind) for c in node.get("content", []))


def parse_created(value: str | None) -> datetime:
    try:
        # Jira format: 2026-10-02T12:00:00.000-0700
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/webhooks/jira")
async def jira_webhook(request: Request, response: Response,
                       x_hub_signature: str | None = Header(default=None)) -> dict:
    body = await request.body()  # raw bytes: the signature is over these exact bytes
    verify_signature(body, x_hub_signature, settings.jira_webhook_secret)

    try:
        event = json.loads(body)
    except ValueError:
        raise HTTPException(400, "body is not JSON")
    kind = event.get("webhookEvent")
    if kind == "jira:issue_updated" and _resolved(event):
        key = (event.get("issue") or {}).get("key")
        if not key:
            raise HTTPException(400, "missing issue key")
        job_id = await run_in_threadpool(_enqueue_index, key)
        response.status_code = 202
        return {"status": "queued", "issue": key, "job": job_id}
    if kind != "jira:issue_created":
        return {"status": "ignored"}

    issue = event.get("issue") or {}
    fields = issue.get("fields") or {}
    key, summary = issue.get("key"), fields.get("summary")
    if not key or not summary:
        raise HTTPException(400, "missing issue key or summary")

    # redact before storing: the queue must not become a second copy of secrets
    clean_summary, c1 = redact(summary)
    clean_desc, c2 = redact(adf_text(fields.get("description") or ""))
    created = parse_created(fields.get("created"))

    payload = {"key": key, "summary": clean_summary, "description": clean_desc,
               "created_at": created.isoformat(), "redactions": dict(c1 + c2)}
    inserted, job_id = await run_in_threadpool(_record_created, payload, created)
    response.status_code = 202 if inserted else 200
    return {"status": "queued" if inserted else "duplicate", "issue": key, "job": job_id}


def _enqueue_index(key: str) -> int:
    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        return enqueue(conn, "index_ticket", {"key": key}, dedupe_key=f"index:{key}")


def _record_created(payload: dict, created: datetime) -> tuple[bool, int | None]:
    key = payload["key"]
    with psycopg.connect(settings.database_url, autocommit=True) as conn, conn.transaction():
        # triages.issue_key is the once-per-issue check: Jira retries deliveries
        inserted = conn.execute(
            """insert into triages (issue_key, summary, description, created_at)
               values (%s, %s, %s, %s)
               on conflict (issue_key) do nothing
               returning issue_key""",
            (key, payload["summary"], payload["description"], created)).fetchone()
        if not inserted:
            return False, None
        job_id = enqueue(conn, "triage_ticket", payload, dedupe_key=f"triage:{key}")
        conn.execute("update triages set job_id = %s where issue_key = %s", (job_id, key))
        return True, job_id


def _resolved(event: dict) -> bool:
    """An issue_updated event whose changelog sets a resolution (not one that clears it)."""
    items = (event.get("changelog") or {}).get("items") or []
    return any(i.get("field") == "resolution" and (i.get("to") or i.get("toString")) for i in items)
