"""Step events: what each step of a running job did, for the dashboard.

The worker sets `current_job` before calling a handler, so pipeline code calls record_step()
without passing job ids around. Outside a job (CLI scripts, evals) record_step does nothing.
Every string is redacted and each input/output is capped at about 8 KB."""
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from triagerag.config import settings
from triagerag.shared.redact import redact

current_job: ContextVar[int | None] = ContextVar("current_job", default=None)

MAX_BYTES = 8_000
_conns: dict[str, psycopg.Connection] = {}


def _conn() -> psycopg.Connection:
    """Its own autocommit connection, so events show up live while the job is still running."""
    url = settings.database_url
    conn = _conns.get(url)
    if conn is None or conn.closed:
        conn = _conns[url] = psycopg.connect(url, autocommit=True)
    return conn


def _redact_all(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {str(k): _redact_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_all(v) for v in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))[0]


def _shorten(value: Any, max_chars: int) -> Any:
    if isinstance(value, str) and len(value) > max_chars:
        return value[:max_chars] + f"… [{len(value) - max_chars} chars cut]"
    if isinstance(value, dict):
        return {k: _shorten(v, max_chars) for k, v in value.items()}
    if isinstance(value, list):
        return [_shorten(v, max_chars) for v in value]
    return value


def cap(value: Any, max_bytes: int = MAX_BYTES) -> Any:
    """Redact, then cut the longest strings until the JSON fits in max_bytes."""
    value = _redact_all(value)
    max_chars = 4_000
    while len(json.dumps(value, default=str)) > max_bytes and max_chars > 50:
        value = _shorten(value, max_chars)
        max_chars //= 2
    text = json.dumps(value, default=str)
    if len(text) > max_bytes:
        return {"truncated": text[:max_bytes]}
    return value


def record_step(step: str, input: Any = None, output: Any = None, ms: int | None = None,
                tokens: int | None = None, status: str = "ok") -> None:
    job_id = current_job.get()
    if job_id is None:
        return
    try:
        _conn().execute(
            """insert into job_events (job_id, seq, step, status, ms, tokens, input, output)
               select %(job)s, coalesce(max(seq), 0) + 1, %(step)s, %(status)s, %(ms)s, %(tokens)s,
                      %(input)s, %(output)s
               from job_events where job_id = %(job)s""",
            {"job": job_id, "step": step, "status": status, "ms": ms, "tokens": tokens,
             "input": Jsonb(cap(input)) if input is not None else None,
             "output": Jsonb(cap(output)) if output is not None else None})
    except psycopg.Error as e:  # observability must never fail the job itself
        print(f"record_step({step}) failed: {e}", flush=True)


@contextmanager
def timed(step: str, input: Any = None) -> Iterator[dict[str, Any]]:
    """Record a step with its duration. Set rec["output"] / rec["tokens"] / rec["status"] inside.
    An exception records status=error, then propagates."""
    rec: dict[str, Any] = {"input": input, "output": None, "tokens": None, "status": "ok"}
    start = time.perf_counter()
    try:
        yield rec
    except Exception as e:
        rec["status"] = "error"
        rec["output"] = {"error": f"{type(e).__name__}: {e}"}
        raise
    finally:
        if current_job.get() is not None:
            record_step(step, rec["input"], rec["output"], int((time.perf_counter() - start) * 1000),
                        rec["tokens"], rec["status"])
