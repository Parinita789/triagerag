from dataclasses import dataclass, field
from datetime import datetime

import psycopg
from pgvector.psycopg import register_vector

from triagerag.config import settings
from triagerag.index.embed import MODEL_NAME, QUERY_PREFIX, model
from triagerag.jobs.events import current_job, record_step, step
from triagerag.query.BM25 import BM25Index, tokenize
from triagerag.query.fuse import RRF_K, rrf
from triagerag.query.rerank import RERANK_MODEL, rerank_scores
from triagerag.query.retrieve import CHUNK_POOL, dense

FUSION_DEPTH = 50
RERANK_DEPTH = 10   # hybrid results shown (and fed to the reranker preview); the top k go to the LLM
SNIPPET_CHARS = 400
DESCRIPTION_CHARS = 1500
COMMENT_CHARS = 600
MAX_COMMENTS = 6


def _rank(ranking: list[str], key: str) -> int | None:
    return ranking.index(key) + 1 if key in ranking else None


def _top(hits: list[tuple[str, float]], n: int = 10) -> list[dict]:
    return [{"key": key, "rank": i, "score": round(score, 4)} for i, (key, score) in enumerate(hits[:n], 1)]


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
        self.preview_rerank = settings.preview_rerank

    def search(self, text: str, before: datetime, exclude: str, k: int = 5) -> list[TicketHit]:
        with step("embed", {"model": MODEL_NAME, "text": text}) as st:
            vec = model().encode(QUERY_PREFIX + text, normalize_embeddings=True)
            st.output = {"dims": len(vec), "first_values": [round(float(x), 4) for x in vec[:6]],
                         "note": "bge query prefix added; normalized, so cosine = dot product"}
        with step("dense", {"pool": CHUNK_POOL, "before": before}) as st:
            dense_hits = dense(self.conn, vec, before, exclude, k=FUSION_DEPTH)
            st.output = _top(dense_hits)
        with step("bm25", {"tokens": tokenize(text)[:60]}) as st:
            bm25_hits = self.bm25.search(text, before, exclude, k=FUSION_DEPTH)
            st.output = _top(bm25_hits)
        d = [t for t, _ in dense_hits]
        b = [t for t, _ in bm25_hits]
        with step("hybrid", {"method": f"reciprocal rank fusion, k={RRF_K}", "depth": FUSION_DEPTH}) as st:
            fused = rrf([d, b], top=max(RERANK_DEPTH, k))
            keys = fused[:k]
            st.output = [{"key": key, "rank": i, "dense_rank": _rank(d, key), "bm25_rank": _rank(b, key),
                          "sent_to_llm": i <= k} for i, key in enumerate(fused, 1)]
        self._preview_rerank(text, vec, before, fused)
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

    def _preview_rerank(self, text: str, vec, before: datetime, fused: list[str]) -> None:
        """Run the cross-encoder on the hybrid results and record how it would reorder them.
        Display only: Phase 5 measured it making retrieval worse, so the LLM keeps the hybrid order."""
        if current_job.get() is None:
            return  # no job to show it in (evals, CLI): don't pay for the model
        if not self.preview_rerank or not fused:
            record_step("rerank", output={"skipped": "reranker preview is off"}, status="skipped")
            return
        with step("rerank", {"model": RERANK_MODEL, "candidates": len(fused)}) as st:
            scores = rerank_scores(self.conn, text, vec, before, fused)
            st.output = {"used": False,
                         "why": "measured in Phase 5: made retrieval worse; the LLM gets the hybrid order",
                         "ranking": [{"key": key, "rank": i, "hybrid_rank": _rank(fused, key),
                                      "score": round(score, 3)} for i, (key, score) in enumerate(scores, 1)]}

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