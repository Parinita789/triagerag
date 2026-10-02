from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from pgvector.psycopg import register_vector

from triagerag.index.embed import QUERY_PREFIX, model
from triagerag.query.BM25 import BM25Index
from triagerag.query.fuse import rrf
from triagerag.query.retrieve import dense

FUSION_DEPTH = 50
SNIPPET_CHARS = 400
DESCRIPTION_CHARS = 1500
COMMENT_CHARS = 600
MAX_COMMENTS = 6


@dataclass
class TicketHit:
    key: str
    summary: str
    issue_type: str | None
    resolution: str | None
    resolved_at: datetime | None
    snippet: str


@dataclass
class TicketDetail:
    key: str
    summary: str
    issue_type: str | None
    resolution: str | None
    resolved_at: datetime | None
    description: str
    comments: list[str] = field(default_factory=list)


class SearchService:
    """Retrieval for the agent. Every method takes `before`, and nothing written
    on or after that time is ever returned."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn
        register_vector(conn)
        conn.execute("set enable_indexscan = off")   # exact vector search, same as the eval
        self.bm25 = BM25Index(conn)

    def search(self, text: str, before: datetime, exclude: str, k: int = 5) -> list[TicketHit]:
        vec = model().encode(QUERY_PREFIX + text, normalize_embeddings=True)
        d = [t for t, _ in dense(self.conn, vec, before, exclude, k=FUSION_DEPTH)]
        b = [t for t, _ in self.bm25.search(text, before, exclude, k=FUSION_DEPTH)]
        keys = rrf([d, b], top=k)
        if not keys:
            return []

        rows = self.conn.execute(
            """select t.key, t.summary, t.issue_type, t.resolution, t.resolved_at, best.content
               from tickets t
               join lateral (
                   select c.content from chunks c
                   where c.ticket_key = t.key and c.created_at < %(before)s
                   order by c.embedding <=> %(q)s
                   limit 1
               ) best on true
               where t.key = any(%(keys)s)""",
            {"before": before, "q": vec, "keys": keys},
        ).fetchall()
        by_key = {r[0]: TicketHit(r[0], r[1], r[2], r[3], r[4], r[5][:SNIPPET_CHARS]) for r in rows}
        return [by_key[k] for k in keys if k in by_key]

    def get_ticket(self, key: str, before: datetime, exclude: str) -> TicketDetail | None:
        if key == exclude:
            return None
        row = self.conn.execute(
            """select key, summary, issue_type, resolution, resolved_at from tickets
               where key = %s and resolved_at < %s""",
            (key, before),
        ).fetchone()
        if not row:
            return None

        chunks = self.conn.execute(
            """select section, content from chunks
               where ticket_key = %s and created_at < %s
               order by created_at""",
            (key, before),
        ).fetchall()
        description = next((c for s, c in chunks if s == "problem"), "")
        comments = [c[:COMMENT_CHARS] for s, c in chunks if s == "comment"][:MAX_COMMENTS]
        return TicketDetail(*row, description=description[:DESCRIPTION_CHARS], comments=comments)