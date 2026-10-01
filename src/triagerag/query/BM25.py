import math
import re
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np
import psycopg

from triagerag.query.retrieve import CHUNK_POOL, _group_by_ticket

K1, B = 1.2, 0.75
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
STOPWORDS = frozenset("""
a an and are as at be but by for from has have in is it its of on or that the this to was were
will with not no can if we i you they them there their then than so do does did been being
""".split())


def tokenize(text: str) -> list[str]:
    return [t for t in (m.lower() for m in TOKEN_RE.findall(text)) if t not in STOPWORDS]


class BM25Index:
    def __init__(self, conn: psycopg.Connection) -> None:
        rows = conn.execute(
            """select c.ticket_key, c.content, c.created_at, t.resolved_at
               from chunks c join tickets t on t.key = c.ticket_key"""
        ).fetchall()

        self.tickets = np.array([r[0] for r in rows], dtype=object)
        self.created = np.array([r[2].timestamp() for r in rows])
        self.resolved = np.array([r[3].timestamp() if r[3] else np.inf for r in rows])

        docs = [tokenize(r[1]) for r in rows]
        self.doc_len = np.array([len(d) for d in docs], dtype=float)
        self.avg_len = self.doc_len.mean()

        # inverted index: word -> (which chunks contain it, how many times)
        ids: dict[str, list[int]] = defaultdict(list)
        tfs: dict[str, list[int]] = defaultdict(list)
        for i, doc in enumerate(docs):
            for term, tf in Counter(doc).items():
                ids[term].append(i)
                tfs[term].append(tf)

        n = len(docs)
        self.postings = {t: (np.array(ids[t]), np.array(tfs[t], dtype=float)) for t in ids}
        self.idf = {t: math.log(1 + (n - len(p[0]) + 0.5) / (len(p[0]) + 0.5))
                    for t, p in self.postings.items()}

    def scores(self, tokens: list[str]) -> np.ndarray:
        s = np.zeros(len(self.tickets))
        for term in set(tokens):
            if term not in self.postings:
                continue
            doc_ids, tf = self.postings[term]
            length_norm = 1 - B + B * self.doc_len[doc_ids] / self.avg_len
            s[doc_ids] += self.idf[term] * tf * (K1 + 1) / (tf + K1 * length_norm)
        return s

    def search(self, text: str, before: datetime, exclude: str, k: int = 10) -> list[tuple[str, float]]:
        s = self.scores(tokenize(text))
        b = before.timestamp()
        allowed = (self.created < b) & (self.resolved < b) & (self.tickets != exclude)
        s = np.where(allowed, s, -np.inf)

        pool = min(CHUNK_POOL, len(s) - 1)
        top = np.argpartition(-s, pool)[:pool]
        top = top[np.argsort(-s[top])]
        rows = [(self.tickets[i], float(s[i])) for i in top if s[i] > 0]
        return _group_by_ticket(rows, k)