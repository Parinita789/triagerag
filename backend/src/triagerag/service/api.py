"""Jobs API for the dashboard and for you: enqueue, list, inspect, stream, retry.
Every endpoint needs X-API-Key (the SSE stream also accepts ?key=, since EventSource can't set headers)."""
import asyncio
import hmac
import json
from collections import Counter
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from psycopg.rows import dict_row
from pydantic import BaseModel, Field

from triagerag.config import settings
from triagerag.jobs import queue, registry
from triagerag.shared.redact import redact

POLL_SECONDS = 0.5
EVENT_COLUMNS = "id, attempt, seq, step, status, ms, tokens, input, output, created_at, updated_at"
JOB_COLUMNS = """id, type, status, attempts, max_attempts, dedupe_key, parent_id, payload, result, error,
                 created_at, run_after, started_at, finished_at, heartbeat_at"""


def _check(key: str | None) -> None:
    if not settings.api_key:
        raise HTTPException(401, "API key not configured on the server (set API_KEY)")
    if not key or not hmac.compare_digest(key, settings.api_key):
        raise HTTPException(401, "bad or missing X-API-Key")


def require_key(x_api_key: str | None = Header(default=None)) -> None:
    _check(x_api_key)


def require_key_or_query(x_api_key: str | None = Header(default=None),
                         key: str | None = Query(default=None)) -> None:
    _check(x_api_key or key)


router = APIRouter()


def _conn() -> psycopg.Connection:
    return psycopg.connect(settings.database_url, autocommit=True, row_factory=dict_row)


def redact_payload(value: Any, counts: Counter) -> Any:
    """Redact every string in the payload before it's stored: jobs must not hold secrets."""
    if isinstance(value, str):
        text, c = redact(value)
        counts.update(c)
        return text
    if isinstance(value, dict):
        return {k: redact_payload(v, counts) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_payload(v, counts) for v in value]
    return value


class NewJob(BaseModel):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str | None = None
    delay: float = Field(default=0, ge=0)


@router.post("/jobs", status_code=202, dependencies=[Depends(require_key)])
def create_job(body: NewJob) -> dict:
    try:
        registry.get(body.type)
    except registry.UnknownJobType as e:
        raise HTTPException(400, str(e)) from None
    counts: Counter = Counter()
    payload = redact_payload(body.payload, counts)
    if counts and body.type == "triage_ticket":
        payload["redactions"] = dict(counts)
    with _conn() as conn:
        job_id = queue.enqueue(conn, body.type, payload, dedupe_key=body.dedupe_key, delay=body.delay)
    return {"id": job_id}


@router.get("/jobs", dependencies=[Depends(require_key)])
def list_jobs(status: str | None = None, type: str | None = None, parent_id: int | None = None,
              limit: int = Query(default=50, ge=1, le=500)) -> list[dict]:
    with _conn() as conn:
        jobs = conn.execute(
            f"""select {JOB_COLUMNS},
                       (select count(*) from jobs c where c.parent_id = j.id) as children
                from jobs j
                where (%(status)s::text is null or status = %(status)s)
                  and (%(type)s::text is null or type = %(type)s)
                  and (%(parent)s::bigint is null or parent_id = %(parent)s)
                order by id desc limit %(limit)s""",
            {"status": status, "type": type, "parent": parent_id, "limit": limit}).fetchall()
        # each step's latest status in the job's current attempt: the lifecycle on the board cards
        steps: dict[int, dict[str, str]] = {}
        for r in conn.execute(
                """select distinct on (e.job_id, e.step) e.job_id, e.step, e.status
                   from job_events e join jobs j on j.id = e.job_id and e.attempt = greatest(j.attempts, 1)
                   where e.job_id = any(%s)
                   order by e.job_id, e.step, e.seq desc""", ([j["id"] for j in jobs],)):
            steps.setdefault(r["job_id"], {})[r["step"]] = r["status"]
    for j in jobs:
        j["stages"] = list(_stages(j["type"]))
        j["steps"] = steps.get(j["id"], {})
    return jobs


def _stages(type: str) -> tuple[str, ...]:
    try:
        return registry.get(type).stages
    except registry.UnknownJobType:
        return ()


@router.get("/job-types", dependencies=[Depends(require_key)])
def job_types() -> dict:
    registry.load()
    return {t: {"stages": list(spec.stages), "max_attempts": spec.max_attempts}
            for t, spec in sorted(registry.HANDLERS.items())}


def _job_detail(conn: psycopg.Connection, job_id: int) -> dict:
    job = conn.execute(f"select {JOB_COLUMNS} from jobs where id = %s", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "no such job")
    job["stages"] = list(_stages(job["type"]))
    job["events"] = conn.execute(
        f"select {EVENT_COLUMNS} from job_events where job_id = %s order by seq", (job_id,)).fetchall()
    by_status = {r["status"]: r["n"] for r in conn.execute(
        "select status, count(*) as n from jobs where parent_id = %s group by status", (job_id,))}
    job["children"] = {"total": sum(by_status.values()), **by_status}
    job["triage"] = conn.execute(
        "select issue_key, summary, description, comment, result from triages where job_id = %s",
        (job_id,)).fetchone()
    return job


@router.get("/jobs/{job_id}", dependencies=[Depends(require_key)])
def get_job(job_id: int) -> dict:
    with _conn() as conn:
        return _job_detail(conn, job_id)


@router.post("/jobs/{job_id}/retry", dependencies=[Depends(require_key)])
def retry_job(job_id: int) -> dict:
    with _conn() as conn:
        if not queue.retry(conn, job_id):
            raise HTTPException(409, "job is not failed, or another active job holds its dedupe key")
    return {"id": job_id, "status": "queued"}


@router.get("/metrics/jobs", dependencies=[Depends(require_key)])
def job_metrics() -> dict:
    with _conn() as conn:
        rows = conn.execute("select type, status, count(*) as n from jobs group by type, status").fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        out.setdefault(r["type"], {})[r["status"]] = r["n"]
    return out


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/jobs/{job_id}/stream", dependencies=[Depends(require_key_or_query)])
async def stream_job(job_id: int, request: Request) -> StreamingResponse:
    """Server-Sent Events: each step as `step` when it starts and again when it finishes
    (same seq; the client replaces it), status changes as `status`, then `end` once the job is
    done or failed. Polls every 500 ms."""
    async def events():
        last_update, last_status = None, None
        async with await psycopg.AsyncConnection.connect(
                settings.database_url, autocommit=True, row_factory=dict_row) as conn:
            while not await request.is_disconnected():
                cur = await conn.execute(
                    f"select {JOB_COLUMNS} from jobs where id = %s", (job_id,))
                job = await cur.fetchone()
                if job is None:
                    yield _sse("error", {"error": "no such job"})
                    return
                # one writer per job (its worker), so updated_at only moves forward
                cur = await conn.execute(
                    f"""select {EVENT_COLUMNS} from job_events
                        where job_id = %s and (%s::timestamptz is null or updated_at > %s)
                        order by updated_at, seq""",
                    (job_id, last_update, last_update))
                for ev in await cur.fetchall():
                    last_update = ev["updated_at"]
                    yield _sse("step", ev)
                if job["status"] != last_status:
                    last_status = job["status"]
                    yield _sse("status", job)
                if job["status"] in ("done", "failed"):
                    yield _sse("end", {"id": job_id, "status": job["status"]})
                    return
                await asyncio.sleep(POLL_SECONDS)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
