from datetime import datetime
from functools import lru_cache

import numpy as np
import psycopg
from sentence_transformers import CrossEncoder

RERANK_MODEL = "BAAI/bge-reranker-base"
MAX_QUERY_WORDS = 200
CHUNKS_PER_TICKET = 2


@lru_cache(maxsize=1)
def reranker() -> CrossEncoder:
    return CrossEncoder(RERANK_MODEL, max_length=512)


def rerank(conn: psycopg.Connection, query_text: str, query_vec: np.ndarray,
           before: datetime, candidates: list[str], top: int = 10) -> list[str]:
    return [key for key, _ in rerank_scores(conn, query_text, query_vec, before, candidates)][:top]


def rerank_scores(conn: psycopg.Connection, query_text: str, query_vec: np.ndarray,
                  before: datetime, candidates: list[str]) -> list[tuple[str, float]]:
    """Cross-encoder score per candidate ticket (its best of CHUNKS_PER_TICKET chunks), best first."""
    rows = conn.execute(
        """select ticket_key, content from (
               select c.ticket_key, c.content,
                      row_number() over (partition by c.ticket_key
                                         order by c.embedding <=> %(q)s) as rn
               from chunks c
               where c.ticket_key = any(%(keys)s) and c.created_at < %(before)s
           ) ranked
           where rn <= %(per)s""",
        {"q": query_vec, "keys": candidates, "before": before, "per": CHUNKS_PER_TICKET},
    ).fetchall()
    if not rows:
        return [(key, 0.0) for key in candidates]

    q = " ".join(query_text.split()[:MAX_QUERY_WORDS])
    scores = reranker().predict([(q, content) for _, content in rows], batch_size=8)

    best: dict[str, float] = {}
    for (ticket, _), s in zip(rows, scores):
        best[ticket] = max(best.get(ticket, float("-inf")), float(s))
    return sorted(best.items(), key=lambda kv: kv[1], reverse=True)