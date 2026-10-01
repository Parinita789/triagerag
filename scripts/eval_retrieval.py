import argparse
import json
import time
from pathlib import Path

import psycopg
import torch
from pgvector.psycopg import register_vector

from triagerag.config import settings
from triagerag.eval.metrics import evaluate, recall_by_link_type
from triagerag.eval.queries import EvalQuery, load_set
from triagerag.index.embed import QUERY_PREFIX, model
from triagerag.query.BM25 import BM25Index
from triagerag.query.fuse import rrf
from triagerag.query.rerank import rerank
from triagerag.query.retrieve import dense

OUT = Path("data/eval")
FUSION_DEPTH = 50   # tickets per method fed into fusion
RERANK_DEPTH = 20   # RRF candidates passed to the reranker


def embed_queries(queries: list[EvalQuery]):
    return model().encode([QUERY_PREFIX + q.text for q in queries],
                          normalize_embeddings=True, batch_size=32)


def run_dense(conn, queries, vectors, depth: int) -> dict[str, list[str]]:
    return {q.key: [t for t, _ in dense(conn, v, q.created_at, q.key, k=depth)]
            for q, v in zip(queries, vectors)}


def run_bm25(conn, queries, depth: int) -> dict[str, list[str]]:
    index = BM25Index(conn)
    return {q.key: [t for t, _ in index.search(q.text, q.created_at, q.key, k=depth)]
            for q in queries}


def main() -> None:
    torch.set_num_threads(4)
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=["dev", "test", "known"], default="dev")
    ap.add_argument("--method", choices=["dense", "bm25", "rrf", "rrf_rerank", "rrf_rerank_vote"], default="rrf")
    args = ap.parse_args()

    with psycopg.connect(settings.database_url) as conn:
        register_vector(conn)
        conn.execute("set enable_indexscan = off")   # exact search for the eval

        queries = load_set(conn, args.set)
        print(f"[{args.set}] {len(queries)} queries, {sum(len(q.relevant) for q in queries)} relevant links")

        start = time.perf_counter()
        if args.method == "bm25":
            ranked = run_bm25(conn, queries, depth=10)
        else:
            vectors = embed_queries(queries)
            if args.method == "dense":
                ranked = run_dense(conn, queries, vectors, depth=10)
            else:
                d = run_dense(conn, queries, vectors, depth=FUSION_DEPTH)
                b = run_bm25(conn, queries, depth=FUSION_DEPTH)
                ranked = {q.key: rrf([d[q.key], b[q.key]]) for q in queries}
                if args.method in ("rrf_rerank", "rrf_rerank_vote"):
                    for i, (q, v) in enumerate(zip(queries, vectors), 1):
                        candidates = rrf([d[q.key], b[q.key]], top=RERANK_DEPTH)
                        reranked = rerank(conn, q.text, v, q.created_at, candidates, top=RERANK_DEPTH)
                        if args.method == "rrf_rerank":
                            ranked[q.key] = reranked[:10]
                        else:
                            ranked[q.key] = rrf([d[q.key], b[q.key], reranked])
                        if i % 20 == 0:
                            print(f"  reranked {i}/{len(queries)}")
        elapsed = time.perf_counter() - start

    print(f"\n{args.set}/{args.method} ({elapsed / len(queries) * 1000:.0f} ms/query, incl. setup)")
    for name, value in evaluate(queries, ranked).items():
        print(f"  {name}: {value:.3f}")
    print("  by link type:", recall_by_link_type(queries, ranked))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{args.set}_{args.method}.json").write_text(json.dumps(ranked, indent=1))


if __name__ == "__main__":
    main()