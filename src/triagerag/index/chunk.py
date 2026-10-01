from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from triagerag.shared.clean import CleanText, CleanTicket, parse_jira_ts
from triagerag.shared.clean import CleanText, CleanTicket
from triagerag.shared.extract import extract
import re

CHATTER_RE = re.compile(
    r"(?i)(\bcommitted\b|\+1\b|\blgtm\b|thanks? (?:you )?for the (?:patch|review|contribution)|looks good)"
)
MAX_CHATTER_CHARS = 200

MAX_WORDS = 350
OVERLAP_WORDS = 50
MIN_COMMENT_CHARS = 40
MAX_BLOCK_LINES = 30


@dataclass
class Chunk:
    ticket_key: str
    section: str
    content: str
    template_ids: list[int]
    exceptions: list[str]
    created_at: datetime


def render(text: CleanText) -> str:
    """Prose plus code blocks, each block cut to MAX_BLOCK_LINES."""
    parts = [text.prose] if text.prose else []
    for block in text.blocks:
        lines = block.splitlines()
        if len(lines) > MAX_BLOCK_LINES:
            lines = lines[:MAX_BLOCK_LINES] + [f"... ({len(lines) - MAX_BLOCK_LINES} more lines)"]
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _paragraphs(body: str) -> Iterator[str]:
    """Paragraphs, with any single paragraph over MAX_WORDS cut into overlapping pieces."""
    for p in body.split("\n\n"):
        words = p.split()
        if not words:
            continue
        if len(words) <= MAX_WORDS:
            yield p
        else:
            step = MAX_WORDS - OVERLAP_WORDS
            for i in range(0, len(words), step):
                yield " ".join(words[i:i + MAX_WORDS])


def _split(body: str) -> list[str]:
    """Pack paragraphs into pieces of at most MAX_WORDS, carrying a short paragraph over as overlap."""
    pieces: list[str] = []
    current: list[str] = []
    count = 0
    for p in _paragraphs(body):
        n = len(p.split())
        if current and count + n > MAX_WORDS:
            pieces.append("\n\n".join(current))
            tail = current[-1]
            tail_n = len(tail.split())
            current, count = ([tail], tail_n) if tail_n <= OVERLAP_WORDS else ([], 0)
        current.append(p)
        count += n
    if current:
        pieces.append("\n\n".join(current))
    return pieces


def _is_chatter(rendered: str) -> bool:
    return len(rendered) < MAX_CHATTER_CHARS and bool(CHATTER_RE.search(rendered))


def chunk_ticket(t: CleanTicket) -> list[Chunk]:
    header = f"[{t.key}] {t.summary}"
    human_comments = []
    for c in t.comments:
        rendered = render(c.text)
        if len(rendered) >= MIN_COMMENT_CHARS and not _is_chatter(rendered):
            human_comments.append(c)

    sections: list[tuple[str, list[CleanText], str]] = [("problem", [t.description], t.created)]
    sections += [("comment", [c.text], c.created) for c in human_comments]

    chunks: list[Chunk] = []
    for section, texts, created in sections:
        template_ids: set[int] = set()
        exceptions: set[str] = set()
        for text in texts:
            ex = extract(text)
            template_ids |= ex.template_ids
            exceptions |= ex.exception_classes

        body = "\n\n".join(render(text) for text in texts).strip()
        for piece in _split(body) or [""]:
            content = f"{header}\n\n{piece}" if piece else header
            chunks.append(Chunk(t.key, section, content, sorted(template_ids),
            sorted(exceptions), parse_jira_ts(created)))
    return chunks