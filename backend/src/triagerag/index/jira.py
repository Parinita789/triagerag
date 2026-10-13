import json
import time
from pathlib import Path
from datetime import datetime
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from triagerag.config import settings

import httpx

BASE = "https://issues.apache.org/jira/rest/api/2/search"
JQL = "project = HDFS AND resolution is not EMPTY ORDER BY created ASC"
RAW_DIR = Path("data/jira/raw_all")
FIELDS = ",".join([
    "summary", "description", "comment", "issuelinks", "components",
    "issuetype", "status", "resolution", "created", "resolutiondate",
])
PAGE_SIZE = 100


def fetch_all(sleep_s: float = 1.0) -> int:
    """Page through all matching tickets, saving each page as JSON.
    Skips pages already on disk, so it's safe to re-run after a failure."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=60) as client:
        start = 0
        total = None
        while total is None or start < total:
            out = RAW_DIR / f"page_{start:05d}.json"

            if out.exists():
                data = json.loads(out.read_text())
                total = data["total"]
                print(f"  skip {out.name} (cached)")
            else:
                data = _get_page(client, start)
                out.write_text(json.dumps(data))
                total = data["total"]
                print(f"  fetched {start}–{start + len(data['issues'])} of {total}")
                time.sleep(sleep_s)

            start += PAGE_SIZE

    return total


def _get_page(client: httpx.Client, start: int, retries: int = 3) -> dict:
    params = {"jql": JQL, "startAt": start, "maxResults": PAGE_SIZE, "fields": FIELDS}
    for attempt in range(1, retries + 1):
        try:
            r = client.get(BASE, params=params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError as e:
            if attempt == retries:
                raise
            wait = 5 * attempt
            print(f"  error on page {start}: {e} — retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


JIRA_TS = "%Y-%m-%dT%H:%M:%S.%f%z"   # e.g. 2026-09-21T02:47:21.000+0000


def _ts(value: str | None) -> datetime | None:
    return datetime.strptime(value, JIRA_TS) if value else None


def _ticket_row(issue: dict[str, Any]) -> tuple:
    f = issue["fields"]
    return (
        issue["key"],
        f["summary"],
        f.get("description"),
        (f.get("status") or {}).get("name"),
        (f.get("resolution") or {}).get("name"),
        (f.get("issuetype") or {}).get("name"),
        [c["name"] for c in f.get("components") or []],
        _ts(f.get("created")),
        _ts(f.get("resolutiondate")),
        Jsonb(issue),
    )


def _link_rows(issue: dict[str, Any]) -> list[tuple[str, str, str]]:
    rows = []
    for link in issue["fields"].get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other:
            rows.append((issue["key"], other["key"], link["type"]["name"]))
    return rows


def load_all() -> tuple[int, int]:
    """Parse every saved page into tickets and ticket_links. Safe to re-run."""
    tickets: list[tuple] = []
    links: list[tuple[str, str, str]] = []

    for page in sorted(RAW_DIR.glob("page_*.json")):
        for issue in json.loads(page.read_text())["issues"]:
            tickets.append(_ticket_row(issue))
            links.extend(_link_rows(issue))

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into tickets (key, summary, description, status, resolution,
                                        issue_type, components, created_at, resolved_at, raw)
                   values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   on conflict (key) do nothing""",
                tickets,
            )
            cur.executemany(
                """insert into ticket_links (from_key, to_key, link_type)
                   values (%s, %s, %s)
                   on conflict do nothing""",
                links,
            )
        conn.commit()

    return len(tickets), len(links)