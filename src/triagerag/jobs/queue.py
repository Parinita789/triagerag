"""A Bull-style job queue on Postgres. Every function takes an autocommit connection, so each
state change is visible to other workers the moment it happens."""
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb

from triagerag.jobs import registry

BACKOFF_SECONDS = 30  # retry n waits 30s × 2^attempts
STALE_MINUTES = 5


def _execute(conn: psycopg.Connection, query: str, params: Any = None) -> psycopg.Cursor:
    # tuple rows whatever the connection's row factory, so callers can pass any connection
    return conn.cursor(row_factory=tuple_row).execute(query, params)


@dataclass
class Job:
    id: int
    type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    parent_id: int | None = None


def enqueue(conn: psycopg.Connection, type: str, payload: dict | None = None, *,
            dedupe_key: str | None = None, delay: float = 0, max_attempts: int | None = None,
            parent_id: int | None = None) -> int:
    """Add a job. With dedupe_key, at most one queued/running job per key exists: a second
    enqueue returns the active job's id instead of adding another."""
    spec = registry.get(type)  # unknown types fail here, not later in a worker
    params = {"type": type, "payload": Jsonb(payload or {}), "dedupe": dedupe_key,
              "delay": delay, "max": max_attempts or spec.max_attempts, "parent": parent_id}
    row = _execute(conn,
        """insert into jobs (type, payload, dedupe_key, run_after, max_attempts, parent_id)
           values (%(type)s, %(payload)s, %(dedupe)s, now() + make_interval(secs => %(delay)s),
                   %(max)s, %(parent)s)
           on conflict (dedupe_key) where status in ('queued', 'running') do nothing
           returning id""", params).fetchone()
    if row:
        return row[0]
    existing = _execute(conn,
        "select id from jobs where dedupe_key = %s and status in ('queued', 'running')",
        (dedupe_key,)).fetchone()
    if existing is None:  # the active job finished between the two statements: try once more
        return enqueue(conn, type, payload, dedupe_key=dedupe_key, delay=delay,
                       max_attempts=max_attempts, parent_id=parent_id)
    return existing[0]


CLAIM = """
update jobs set status = 'running', attempts = attempts + 1,
                started_at = now(), heartbeat_at = now(), error = null
where id = (
  select id from jobs
  where status = 'queued' and run_after <= now() and type = any(%(types)s)
  order by run_after, id
  for update skip locked
  limit 1)
returning id, type, payload, attempts, max_attempts, parent_id"""


def claim(conn: psycopg.Connection, types: list[str]) -> Job | None:
    row = _execute(conn, CLAIM, {"types": types}).fetchone()
    return Job(*row) if row else None


def complete(conn: psycopg.Connection, job_id: int, result: dict | None = None) -> None:
    _execute(conn, """update jobs set status = 'done', finished_at = now(), result = %s
                    where id = %s""", (Jsonb(result) if result is not None else None, job_id))


def fail(conn: psycopg.Connection, job_id: int, error: str) -> str:
    """Retry later with exponential backoff, or give up after max_attempts. Returns the new status."""
    return _execute(conn,
        """update jobs
           set status = case when attempts >= max_attempts then 'failed' else 'queued' end,
               run_after = now() + make_interval(secs => %(base)s * power(2, attempts)),
               finished_at = case when attempts >= max_attempts then now() end,
               error = %(error)s
           where id = %(id)s
           returning status""",
        {"id": job_id, "error": error[:2000], "base": BACKOFF_SECONDS}).fetchone()[0]


def heartbeat(conn: psycopg.Connection, job_id: int) -> None:
    _execute(conn, "update jobs set heartbeat_at = now() where id = %s", (job_id,))


def requeue_stale(conn: psycopg.Connection, minutes: float = STALE_MINUTES) -> int:
    """A job is dead when its heartbeat stopped, not when it has merely run for a long time."""
    cur = _execute(conn,
        """update jobs
           set status = case when attempts >= max_attempts then 'failed' else 'queued' end,
               finished_at = case when attempts >= max_attempts then now() end,
               error = 'worker stopped mid-job (no heartbeat)'
           where status = 'running' and heartbeat_at < now() - make_interval(secs => %s)""",
        (minutes * 60,))
    return cur.rowcount


def retry(conn: psycopg.Connection, job_id: int) -> bool:
    """Put a failed job back in the queue with fresh attempts. False if it isn't failed, or if
    another active job already holds its dedupe_key."""
    try:
        cur = _execute(conn,
            """update jobs set status = 'queued', attempts = 0, run_after = now(),
                               finished_at = null, error = null
               where id = %s and status = 'failed'""", (job_id,))
    except psycopg.errors.UniqueViolation:
        return False
    return cur.rowcount == 1
