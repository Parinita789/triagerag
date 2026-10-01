import re
from dataclasses import dataclass, field
from datetime import datetime

import psycopg

from triagerag.index.chunk import render
from triagerag.shared.clean import clean_ticket

LINK_TYPES = ["Reference", "Regression", "Problem/Incident"]
TICKET_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")

CUTOFF = "2018-12-06"      # test queries: fixed bugs filed on or after this date
DEV_START = "2016-12-06"   # dev queries: fixed bugs filed between this date and CUTOFF


@dataclass
class EvalQuery:
    key: str
    created_at: datetime
    text: str
    relevant: dict[str, str] = field(default_factory=dict)   # linked ticket key -> link type


def query_text(issue: dict) -> str:
    """What the ticket looked like when filed: summary + description. Ticket keys removed."""
    t = clean_ticket(issue)
    text = f"{t.summary}\n\n{render(t.description)}".strip()
    return TICKET_KEY_RE.sub("", text)


def load_queries(conn: psycopg.Connection, created_from: str, created_to: str | None = None) -> list[EvalQuery]:
    rows = conn.execute(
        """select q.key, q.created_at, q.raw, l.to_key, l.link_type
           from tickets q
           join ticket_links l on l.from_key = q.key and l.link_type = any(%(types)s)
           join tickets i on i.key = l.to_key and i.resolved_at < q.created_at
           where q.issue_type = 'Bug' and q.resolution = 'Fixed'
             and q.created_at >= %(start)s
             and (%(end)s::timestamptz is null or q.created_at < %(end)s)""",
        {"types": LINK_TYPES, "start": created_from, "end": created_to},
    ).fetchall()

    queries: dict[str, EvalQuery] = {}
    for key, created_at, raw, to_key, link_type in rows:
        if key not in queries:
            queries[key] = EvalQuery(key, created_at, query_text(raw))
        queries[key].relevant[to_key] = link_type
    return list(queries.values())


def load_test_queries(conn: psycopg.Connection) -> list[EvalQuery]:
    return load_queries(conn, CUTOFF)


def load_dev_queries(conn: psycopg.Connection) -> list[EvalQuery]:
    return load_queries(conn, DEV_START, CUTOFF)


def load_set(conn: psycopg.Connection, name: str) -> list[EvalQuery]:
    if name == "dev":
        return load_dev_queries(conn)
    if name == "known":
        return load_known_issue_queries(conn)
    return load_test_queries(conn)


def load_known_issue_queries(conn: psycopg.Connection) -> list[EvalQuery]:
    """Tickets closed as duplicates of a bug that was already fixed when they were filed."""
    rows = conn.execute(
        """select d.key, d.created_at, d.raw, l.to_key
           from tickets d
           join ticket_links l on l.from_key = d.key and l.link_type = 'Duplicate'
           join tickets a on a.key = l.to_key and a.resolved_at < d.created_at
           where d.resolution = 'Duplicate' and d.created_at >= %s""",
        (CUTOFF,),
    ).fetchall()

    queries: dict[str, EvalQuery] = {}
    for key, created_at, raw, to_key in rows:
        if key not in queries:
            queries[key] = EvalQuery(key, created_at, query_text(raw))
        queries[key].relevant[to_key] = "Duplicate"
    return list(queries.values())