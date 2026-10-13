"""Step events: the lifecycle of a running job, step by step, for the dashboard.

The worker sets `current_job` / `current_attempt` before calling a handler, so pipeline code
records steps without passing ids around. Outside a job (CLI scripts, evals) every call here
does nothing. A step is written as 'running' when it starts and updated when it ends, so the
dashboard shows which step a job is in right now. Every string is redacted and each
input/output is capped at about 8 KB."""
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
current_attempt: ContextVar[int] = ContextVar("current_attempt", default=1)

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


def _json(value: Any) -> Jsonb | None:
    return Jsonb(cap(value)) if value is not None else None


def _insert(step: str, status: str, input: Any, output: Any, ms: int | None, tokens: int | None) -> int | None:
    job_id = current_job.get()
    if job_id is None:
        return None
    try:
        return _conn().execute(
            """insert into job_events (job_id, attempt, seq, step, status, ms, tokens, input, output)
               select %(job)s, %(attempt)s, coalesce(max(seq), 0) + 1, %(step)s, %(status)s, %(ms)s,
                      %(tokens)s, %(input)s, %(output)s
               from job_events where job_id = %(job)s
               returning id""",
            {"job": job_id, "attempt": current_attempt.get(), "step": step, "status": status,
             "ms": ms, "tokens": tokens, "input": _json(input), "output": _json(output)}).fetchone()[0]
    except psycopg.Error as e:  # observability must never fail the job itself
        print(f"step {step} not recorded: {e}", flush=True)
        return None


def _update(event_id: int | None, **fields: Any) -> None:
    if event_id is None:
        return
    for key in ("input", "output"):
        if key in fields:
            fields[key] = _json(fields[key])
    sets = ", ".join(f"{k} = %({k})s" for k in fields)
    try:
        _conn().execute(f"update job_events set {sets}, updated_at = clock_timestamp() where id = %(id)s",
                        {**fields, "id": event_id})
    except psycopg.Error as e:
        print(f"step update not recorded: {e}", flush=True)


def record_step(step: str, input: Any = None, output: Any = None, ms: int | None = None,
                tokens: int | None = None, status: str = "ok") -> None:
    """A step that's already finished (or instant)."""
    _insert(step, status, input, output, ms, tokens)


class Step:
    """A step in progress. Set .output / .tokens / .status before it ends; progress() shows
    partial output while it's still running (for long steps like pushing logs)."""

    def __init__(self, name: str, input: Any) -> None:
        self.name, self.input = name, input
        self.output: Any = None
        self.tokens: int | None = None
        self.status = "ok"
        self.start = time.perf_counter()
        self.id = _insert(name, "running", input, None, None, None)

    def progress(self, output: Any) -> None:
        _update(self.id, output=output, ms=self.ms)

    @property
    def ms(self) -> int:
        return int((time.perf_counter() - self.start) * 1000)


@contextmanager
def step(name: str, input: Any = None) -> Iterator[Step]:
    """Record a step: 'running' now, then ok / error / skipped with its duration when the block ends.
    An exception marks it error and propagates."""
    s = Step(name, input)
    try:
        yield s
    except Exception as e:
        s.status = "error"
        s.output = {**(s.output if isinstance(s.output, dict) else {}), "error": f"{type(e).__name__}: {e}"}
        raise
    finally:
        _update(s.id, status=s.status, output=s.output, ms=s.ms, tokens=s.tokens)
