"""The job currently running in this worker. Pipeline code reads it instead of passing job ids around."""
from contextvars import ContextVar

current_job: ContextVar[int | None] = ContextVar("current_job", default=None)
