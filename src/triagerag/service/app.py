"""Jira webhook receiver: verify the signature, record the issue once, return immediately.
The slow part (retrieval + LLM) runs in a separate worker that reads triage_jobs."""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import psycopg
from fastapi import FastAPI, Header, HTTPException, Request, Response

from triagerag.config import settings
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
    if event.get("webhookEvent") != "jira:issue_created":
        return {"status": "ignored"}

    issue = event.get("issue") or {}
    fields = issue.get("fields") or {}
    key, summary = issue.get("key"), fields.get("summary")
    if not key or not summary:
        raise HTTPException(400, "missing issue key or summary")

    # redact before storing: the queue table must not become a second copy of secrets
    clean_summary = redact(summary)[0]
    clean_desc = redact(adf_text(fields.get("description") or ""))[0]

    async with await psycopg.AsyncConnection.connect(settings.database_url) as conn:
        cur = await conn.execute(
            """insert into triage_jobs (issue_key, summary, description, created_at)
               values (%s, %s, %s, %s)
               on conflict (issue_key) do nothing
               returning issue_key""",
            (key, clean_summary, clean_desc, parse_created(fields.get("created"))))
        inserted = await cur.fetchone()

    response.status_code = 202 if inserted else 200
    return {"status": "queued" if inserted else "duplicate", "issue": key}