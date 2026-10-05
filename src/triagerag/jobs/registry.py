"""Job type → handler. A handler is fn(ctx, payload) -> result dict (or None)."""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class UnknownJobType(ValueError):
    pass


@dataclass(frozen=True)
class JobSpec:
    type: str
    fn: Callable[..., dict[str, Any] | None]
    max_attempts: int


HANDLERS: dict[str, JobSpec] = {}


def job(type: str, max_attempts: int = 3) -> Callable:
    def register(fn: Callable) -> Callable:
        HANDLERS[type] = JobSpec(type, fn, max_attempts)
        return fn
    return register


def get(type: str) -> JobSpec:
    if type not in HANDLERS:
        load()
    try:
        return HANDLERS[type]
    except KeyError:
        raise UnknownJobType(f"unknown job type {type!r}") from None


def load() -> None:
    """Import the pipeline so its @job decorators run."""
    import triagerag.pipeline  # noqa: F401
