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
        return candidates[:top]

    q = " ".join(query_text.split()[:MAX_QUERY_WORDS])
    scores = reranker().predict([(q, content) for _, content in rows], batch_size=8)

    best: dict[str, float] = {}
    for (ticket, _), s in zip(rows, scores):
        best[ticket] = max(best.get(ticket, float("-inf")), float(s))
    return sorted(best, key=best.__getitem__, reverse=True)[:top]