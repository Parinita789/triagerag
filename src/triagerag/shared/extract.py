import re
from dataclasses import dataclass
from typing import Literal

from triagerag.shared.clean import CleanText
from triagerag.shared.normalize import normalize

LEVEL = r"(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)"
TIMESTAMP = (
    r"(?:\d{6} \d{6}(?: \d+)?"                                        # 081109 203518 143
    r"|\d{2}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}"                           # 08/05/27 11:30:08
    r"|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[,.]\d{3})?)"         # 2013-02-14 17:29:58,128
)
LOG_RE = re.compile(
    rf"^\s*{TIMESTAMP}\s+(?:\[[^\]]*\]\s+)?{LEVEL}\s+(?:\[[^\]]*\]\s+)?"
    r"(?P<component>[\w.$-]+):\s*(?P<message>.*)$"
)
EXC_LINE_RE = re.compile(
    r"^\s*(?:Caused by:\s*)?"
    r"(?P<cls>(?:java|javax|org|com|io)\.[\w.$]+(?:Exception|Error|Throwable))\b"
)

Source = Literal["block", "prose"]


@dataclass(frozen=True)
class LogLine:
    source: Source
    message: str              # the text after timestamp/level/component
    template_id: int | None   # None = no known Drain template
    exception: str | None


@dataclass(frozen=True)
class ExceptionLine:
    source: Source
    exception: str            # e.g. java.io.IOException


@dataclass
class Extracted:
    logs: list[LogLine]
    exceptions: list[ExceptionLine]

    @property
    def template_ids(self) -> set[int]:
        return {l.template_id for l in self.logs if l.template_id is not None}

    @property
    def exception_classes(self) -> set[str]:
        return {e.exception for e in self.exceptions} | {
            l.exception for l in self.logs if l.exception
        }


def _scan(text: str, source: Source, out: Extracted) -> None:
    for line in text.splitlines():
        m = LOG_RE.match(line)
        if m:
            n = normalize(m.group("message"))
            out.logs.append(LogLine(source, m.group("message"), n.template_id, n.exception))
            continue
        e = EXC_LINE_RE.match(line)
        if e:
            out.exceptions.append(ExceptionLine(source, e.group("cls")))


def extract(text: CleanText) -> Extracted:
    out = Extracted(logs=[], exceptions=[])
    for block in text.blocks:
        _scan(block, "block", out)
    _scan(text.prose, "prose", out)
    return out


def log_messages(text: CleanText) -> list[tuple[Source, str]]:
    """Every log message found by shape, in blocks and prose. No template lookup."""
    found: list[tuple[Source, str]] = []
    for source, body in [("block", b) for b in text.blocks] + [("prose", text.prose)]:
        for line in body.splitlines():
            m = LOG_RE.match(line)
            if m and re.search(r"[A-Za-z]{3}", m.group("message")):
                found.append((source, m.group("message")))
    return found
