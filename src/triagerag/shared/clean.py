import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from triagerag.shared.redact import redact

BOTS = frozenset({"hudson", "hadoopqa", "githubbot", "genericqa"})

# {code}...{code}, {code:java}...{code}, {noformat}...{noformat}
BLOCK_RE = re.compile(r"\{(code|noformat)(?::[^}]*)?\}(.*?)\{\1\}", re.S)

# applied in order to prose only, never to code blocks
MARKUP_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\{\}"), ""),                                   # {{{}x{}}} artifacts → {{x}}
    (re.compile(r"![^!\n]+!"), ""),                              # images: !file.png|width=..!
    (re.compile(r"\[([^|\]\n]+)\|[^\]\n]+\]"), r"\1"),          # [text|url] → text
    (re.compile(r"\[(https?://[^\]\s]+)\]"), r"\1"),            # [url] → url
    (re.compile(r"\{\{(.*?)\}\}"), r"\1"),                       # {{monospace}} → text
    (re.compile(r"^h[1-6]\.\s*", re.M), ""),                     # h2. Heading → Heading
    (re.compile(r"\{(?:quote|color[^}]*|panel[^}]*)\}"), ""),    # container tags
    (re.compile(r"(?<!\w)\*(\S[^*\n]*?)\*(?!\w)"), r"\1"),       # *bold* → bold
    (re.compile(r"\{\{|\}\}"), ""),                              # stray unbalanced {{ or }}
]


@dataclass
class CleanText:
    prose: str
    blocks: list[str] = field(default_factory=list)


@dataclass
class CleanComment:
    author: str
    created: str
    text: CleanText


@dataclass
class CleanTicket:
    key: str
    summary: str
    description: CleanText
    comments: list[CleanComment]
    redactions: Counter[str]
    bot_comments_dropped: int


def _normalize_whitespace(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)       # trailing spaces
    text = re.sub(r"\n{3,}", "\n\n", text)        # at most one blank line
    return text.strip()


def strip_markup(prose: str) -> str:
    for pattern, replacement in MARKUP_RULES:
        prose = pattern.sub(replacement, prose)
    return _normalize_whitespace(prose)


def split_blocks(text: str) -> tuple[str, list[str]]:
    """Pull code/noformat blocks out of the text. Returns (prose, blocks)."""
    blocks = [m.group(2).strip("\n") for m in BLOCK_RE.finditer(text)]
    prose = BLOCK_RE.sub("\n", text)

    # an opening tag with no closing tag: treat the rest of the text as a block
    UNCLOSED_RE = re.compile(r"\{(?:code|noformat)(?::[^}]*)?\}(.*)\Z", re.S)
    m = UNCLOSED_RE.search(prose)
    if m:
        blocks.append(m.group(1).strip("\n"))
        prose = prose[:m.start()]

    return prose, [b for b in blocks if b.strip()]


def clean_text(raw: str | None) -> tuple[CleanText, Counter[str]]:
    counts: Counter[str] = Counter()
    if not raw:
        return CleanText(prose=""), counts

    raw = raw.replace("\u00a0", " ")
    prose, blocks = split_blocks(raw)
    prose = strip_markup(prose)

    prose, c = redact(prose)
    counts.update(c)
    redacted_blocks = []
    for block in blocks:
        block, c = redact(_normalize_whitespace(block))
        counts.update(c)
        redacted_blocks.append(block)

    return CleanText(prose=prose, blocks=redacted_blocks), counts


def clean_ticket(issue: dict[str, Any]) -> CleanTicket:
    f = issue["fields"]
    counts: Counter[str] = Counter()

    summary, c = redact(f["summary"])
    counts.update(c)

    description, c = clean_text(f.get("description"))
    counts.update(c)

    comments: list[CleanComment] = []
    dropped = 0
    for cm in (f.get("comment") or {}).get("comments", []):
        if cm["author"]["name"] in BOTS:
            dropped += 1
            continue
        text, c = clean_text(cm.get("body"))
        counts.update(c)
        if text.prose or text.blocks:
            comments.append(CleanComment(cm["author"]["name"], cm["created"], text))

    return CleanTicket(issue["key"], summary, description, comments, counts, dropped)