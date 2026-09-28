import re
from dataclasses import dataclass
from functools import lru_cache

from drain3 import TemplateMiner

from triagerag.index.logs.drain import load_miner

EXC_CLASS = re.compile(r"java[\w.$]*(?:Exception|Error)")


@dataclass(frozen=True)
class Normalized:
    template_id: int | None     # None = line matches no known template
    exception: str | None       # e.g. "java.net.SocketTimeoutException"


@lru_cache(maxsize=1)
def _miner() -> TemplateMiner:
    return load_miner()


def normalize(message: str) -> Normalized:
    """Look up a log message against the trained templates. Never learns."""
    cluster = _miner().match(message)
    exc = EXC_CLASS.search(message)
    return Normalized(
        template_id=cluster.cluster_id if cluster else None,
        exception=exc.group(0) if exc else None,
    )