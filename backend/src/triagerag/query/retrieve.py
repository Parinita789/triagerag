from datetime import datetime

import numpy as np
import psycopg

CHUNK_POOL = 200   # chunks fetched before grouping into tickets


def _group_by_ticket(rows: list[tuple[str, float]], k: int) -> list[tuple[str, float]]:
    """Rows arrive best-first; keep each ticket's first (best) chunk score."""
    seen: dict[str, float] = {}
    for ticket, score in rows:
        if ticket not in seen:
            seen[ticket] = score
            if len(seen) == k:
                break
    return list(seen.items())


def dense(conn: psycopg.Connection, query_vec: np.ndarray, before: datetime,
          exclude: str, k: int = 10) -> list[tuple[str, float]]:
    rows = conn.execute(
        """select c.ticket_key, 1 - (c.embedding <=> %(q)s) as score
           from chunks c join tickets t on t.key = c.ticket_key
            where c.created_at < %(before)s and t.resolved_at < %(before)s
            and c.ticket_key <> %(exclude)s
           order by c.embedding <=> %(q)s
           limit %(pool)s""",
        {"q": query_vec, "before": before, "exclude": exclude, "pool": CHUNK_POOL},
    ).fetchall()
    return _group_by_ticket(rows, k)