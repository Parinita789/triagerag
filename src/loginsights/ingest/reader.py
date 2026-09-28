import re
from collections.abc import Iterator
from datetime import datetime

from pydantic import BaseModel

BLOCK_RE = re.compile(r"blk_-?\d+")

# 081109 203518 143 INFO dfs.DataNode$DataXceiver: Receiving block blk_-16089...
LINE_RE = re.compile(
    r"^(?P<date>\d{6})\s+"
    r"(?P<time>\d{6})\s+"
    r"(?P<pid>\d+)\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"(?P<component>\S+?):\s+"
    r"(?P<message>.*\S)\s*$"
)

class LogLine(BaseModel):
    ts: datetime
    pid: int
    level: str
    component: str
    message: str
    block_ids: list[str]

def parse_line(raw: str) -> LogLine | None:
    """Return None for lines that don't match the expected shape."""
    m = LINE_RE.match(raw)
    if m is None:
        return None
    try:
        ts = datetime.strptime(f"{m['date']} {m['time']}", "%y%m%d %H%M%S")
    except ValueError:
        # Matched the digit shape but isn't a real timestamp (e.g. 081350).
        return None
    message = m["message"]
    return LogLine(
        ts=ts,
        pid=int(m["pid"]),
        level=m["level"],
        component=m["component"],
        message=message,
        block_ids=BLOCK_RE.findall(message),
    )

def iter_lines(path: Path) -> Iterator[tuple[int, LogLine | None]]:
    """Stream the file, yielding (line_number, parsed)."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line_number, raw in enumerate(f, start=1):
            yield line_number, parse_line(raw.rstrip("\n"))