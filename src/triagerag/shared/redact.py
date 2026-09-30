import hashlib
import hmac
import math
import re
from collections import Counter
from dataclasses import dataclass

# In production this comes from a secret manager, never from code.
PSEUDONYM_KEY = b"dev-only-key-change-me"


@dataclass(frozen=True)
class Detector:
    kind: str
    pattern: re.Pattern[str]
    group: int = 0  # which regex group to replace; 0 = the whole match


DETECTORS = [
    # multi-line first
    Detector("PRIVATE_KEY", re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    # known formats
    Detector("AWS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    Detector("GITHUB_TOKEN", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    Detector("JWT", re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")),
    # credentials by context: replace only the value (group 2), keep the keyword
    Detector("CREDENTIAL", re.compile(
        r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[:=]\s*([^\s,;]+)"), group=2),
    # user:pass inside URLs, e.g. jdbc:mysql://admin:hunter2@db:3306
    Detector("URL_CREDENTIAL", re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+(?=@)")),
    # structured PII
    Detector("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    Detector("CARD", re.compile(
    r"(?<![\w-])(?:[3-6]\d{3}([ -]?)\d{4}\1\d{4}\1\d{4})(?![\w-])"
)),
]


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number if d.isdigit()]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0



NOT_SECRET_CHARS = set("()<>${}")


def _looks_like_secret(value: str) -> bool:
    """Decide whether the value after a keyword like 'token=' is a real secret."""
    if len(value) < 16:
        return False                      # too short: "No", "$", "null", "xxx"
    if any(c in NOT_SECRET_CHARS for c in value):
        return False                      # code, placeholder, or template: fn(...), <url>, ${var}
    if not (any(c.isdigit() for c in value) and any(c.isalpha() for c in value)):
        return False                      # generated secrets mix letters and digits
    return shannon_entropy(value) > 3.5   # random-looking, not a word or identifier


def _pseudonym(kind: str, value: str) -> str:
    digest = hmac.new(PSEUDONYM_KEY, value.encode(), hashlib.sha256).hexdigest()[:8]
    return f"<{kind}_{digest}>"


def shannon_entropy(s: str) -> float:
    counts = Counter(s)
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in counts.values())

def redact(text: str) -> tuple[str, Counter[str]]:
    """Replace secrets and PII with consistent pseudonyms.
    Returns the redacted text and a count per kind (never the values)."""
    counts: Counter[str] = Counter()
    out = text
    for start, end, kind in reversed(find_spans(text)):
        out = out[:start] + _pseudonym(kind, text[start:end]) + out[end:]
        counts[kind] += 1
    return out, counts

ALLOWED_EMAIL_DOMAINS = ("example.com",)


def find_spans(text: str) -> list[tuple[int, int, str]]:
    """Every accepted match as (start, end, kind), with overlaps resolved."""
    spans: list[tuple[int, int, str]] = []

    for det in DETECTORS:
        for m in det.pattern.finditer(text):
            start, end = m.span(det.group)
            value = m.group(det.group)
            if det.kind == "CARD" and not _luhn_ok(value):
                continue
            if det.kind == "CREDENTIAL" and not _looks_like_secret(value):
                continue
            if det.kind == "EMAIL" and value.lower().endswith(ALLOWED_EMAIL_DOMAINS):
                continue
            spans.append((start, end, det.kind))

    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    kept: list[tuple[int, int, str]] = []
    for s in spans:
        if kept and s[0] < kept[-1][1]:
            continue
        kept.append(s)
    return kept