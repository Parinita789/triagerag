"""Loki log tools. The agent passes structured parameters; LogQL is built here, never by the model."""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

from triagerag.index.logs.drain import build_miner
from triagerag.shared.redact import redact

ROOT = Path(__file__).resolve().parents[3]
MODEL = ROOT / "models" / "drain_hdfs.bin"
BASELINE = ROOT / "models" / "hdfs_template_baseline.json"

MAX_RANGE_S = 6 * 3600   # widest time range one query may cover
MAX_LINES = 4000         # Loki's default per-query cap is 5000
MAX_RAW = 50
RARE = 0.02              # template found in under 2% of blocks
EXPECTED = 0.95          # template found in 95%+ of blocks: part of every block's write path
DATA_END_S = 1226404800  # 2008-11-11 12:00 UTC, end of the HDFS v1 data

BLK_RE = re.compile(r"^blk_-?\d+$")
FORBIDDEN = set('"`\\')


class LogQueryError(ValueError):
    """Returned to the agent as an error result so it can fix its parameters."""


def _ns(value: str) -> int:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # HDFS v1 timestamps are treated as UTC
    return int(dt.timestamp()) * 1_000_000_000


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def validate(start: str, end: str, block_id: str | None, contains: str | None,
             component: str | None, level: str | None, known: dict[str, list[str]]) -> tuple[int, int]:
    try:
        s, e = _ns(start), _ns(end)
    except ValueError:
        raise LogQueryError("start and end must be ISO times, e.g. 2008-11-10T14:20:00Z")
    if e <= s:
        raise LogQueryError("end must be after start")
    if (e - s) / 1e9 > MAX_RANGE_S:
        raise LogQueryError(f"range is longer than {MAX_RANGE_S // 3600}h; narrow it")
    if block_id is not None and not BLK_RE.match(block_id):
        raise LogQueryError("block_id must look like blk_-1608999687919862906")
    if contains is not None and (not 1 <= len(contains) <= 100 or FORBIDDEN & set(contains)):
        raise LogQueryError('contains must be 1-100 characters without " ` or \\')
    for name, value in (("component", component), ("level", level)):
        if value is not None and value not in known[name]:
            raise LogQueryError(f"unknown {name} {value!r}; valid: {', '.join(known[name])}")
    return s, e


def build_logql(block_id: str | None, contains: str | None,
                component: str | None, level: str | None) -> str:
    matchers = [f'component="{component}"'] if component else []
    if level:
        matchers.append(f'level="{level}"')
    q = "{" + (", ".join(matchers) or 'level=~".+"') + "}"
    if block_id:
        # regex so blk_-123 doesn't also match blk_-1234...
        q += f' |~ "{block_id}([^0-9]|$)"'
    if contains:
        q += f' |= "{contains}"'
    return q


class LogTools:
    def __init__(self, url: str = "http://localhost:3100"):
        self.url = url
        self.http = httpx.Client(timeout=60)
        self.miner = build_miner(MODEL)
        raw = json.loads(BASELINE.read_text())
        self.templates = {int(t): v["template"] for t, v in raw.items()}
        self.share = {int(t): v["block_share"] for t, v in raw.items()}
        self.lifecycle = sorted(t for t, s in self.share.items() if s >= EXPECTED)
        self._known: dict[str, list[str]] | None = None

    def known_labels(self) -> dict[str, list[str]]:
        # a metric query, because Loki's label/series APIs came back empty for 2008 data here
        if self._known is None:
            r = self.http.get(f"{self.url}/loki/api/v1/query", params={
                "query": 'count by (component, level) (count_over_time({level=~".+"}[40h]))',
                "time": DATA_END_S})
            r.raise_for_status()
            series = [s["metric"] for s in r.json()["data"]["result"]]
            self._known = {
                "component": sorted({m["component"] for m in series}),
                "level": sorted({m["level"] for m in series}),
            }
        return self._known

    def labels(self) -> str:
        k = self.known_labels()
        return (f"component: {', '.join(k['component'])}\nlevel: {', '.join(k['level'])}\n"
                "data covers 2008-11-09T20:35Z to 2008-11-11T11:00Z (UTC), with gaps")

    def _fetch(self, logql: str, s: int, e: int, limit: int) -> list[tuple[int, str]]:
        r = self.http.get(f"{self.url}/loki/api/v1/query_range", params={
            "query": logql, "start": s, "end": e, "limit": limit, "direction": "forward"})
        if r.status_code != 200:
            raise LogQueryError(f"Loki rejected the query: {r.text[:200]}")
        lines = [(int(ts), line) for st in r.json()["data"]["result"] for ts, line in st["values"]]
        return sorted(lines)

    def template_stats(self, *, start: str, end: str, block_id: str | None = None,
                       contains: str | None = None, component: str | None = None,
                       level: str | None = None) -> str:
        s, e = validate(start, end, block_id, contains, component, level, self.known_labels())
        q = build_logql(block_id, contains, component, level)
        lines = self._fetch(q, s, e, MAX_LINES + 1)
        if len(lines) > MAX_LINES:
            raise LogQueryError(f"more than {MAX_LINES} lines match; add block_id or contains, "
                                "a component or level, or use a shorter range")

        counts: Counter = Counter()
        unmatched: list[str] = []
        for _, line in lines:
            parts = line.split(" ", 5)
            msg = parts[5] if len(parts) == 6 else line
            cluster = self.miner.match(msg, full_search_strategy="fallback")
            if cluster is None:
                unmatched.append(line)
            else:
                counts[cluster.cluster_id] += 1

        out = [f"logql: {q}", f"range: {_iso(s)} to {_iso(e)}   lines: {len(lines)}"]
        if not lines:
            out.append("no lines matched")
            return "\n".join(out)

        out.append("templates, rarest first (count | share of blocks that normally have it):")
        for tid, n in sorted(counts.items(), key=lambda kv: self.share.get(kv[0], 0)):
            share = self.share.get(tid, 0)
            flag = "RARE " if share < RARE else ""
            out.append(f"  T{tid} x{n} | {share:.1%} | {flag}{self.templates.get(tid, '?')}")

        if block_id:
            seen = set(counts)
            if seen & set(self.lifecycle):
                missing = [t for t in self.lifecycle if t not in seen]
                for t in missing:
                    out.append(f"  MISSING T{t} | normally in {self.share[t]:.0%} of blocks | {self.templates[t]}")
                if not missing:
                    out.append("  no expected write-path lines are missing")
            else:
                out.append("  note: none of this block's write-path lines are in range; "
                           "widen the range before concluding anything is missing")

        if unmatched:
            out.append(f"unmatched lines: {len(unmatched)}, e.g. {redact(unmatched[0])[0][:200]}")
        return "\n".join(out)

    def raw_lines(self, *, start: str, end: str, limit: int = 20, block_id: str | None = None,
                  contains: str | None = None, component: str | None = None,
                  level: str | None = None) -> str:
        s, e = validate(start, end, block_id, contains, component, level, self.known_labels())
        q = build_logql(block_id, contains, component, level)
        lines = self._fetch(q, s, e, max(1, min(limit, MAX_RAW)))
        if not lines:
            return f"logql: {q}\nno lines matched"
        return "\n".join([f"logql: {q}"] + [redact(line)[0][:300] for _, line in lines])