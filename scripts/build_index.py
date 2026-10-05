"""Chunk, embed, and store tickets. Resumable. Same code as the reindex_all / index_batch jobs."""
import argparse
import time

import psycopg
import torch

from triagerag.config import settings
from triagerag.index.embed import model
from triagerag.pipeline.index import index_keys, unindexed_keys


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild", action="store_true", help="delete all chunks and start over")
    ap.add_argument("--limit", type=int, help="only process this many tickets (for timing)")
    ap.add_argument("--threads", type=int, default=4, help="CPU threads for the model")
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    print("model device:", model().device)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        if args.rebuild:
            conn.execute("truncate chunks")
        todo = unindexed_keys(conn)
        if args.limit:
            todo = todo[:args.limit]
        print(f"{len(todo):,} tickets to index")

        start = time.perf_counter()

        def progress(done: int, n_chunks: int) -> None:
            elapsed = time.perf_counter() - start
            print(f"  {done:,}/{len(todo):,} tickets · {n_chunks:,} chunks · "
                  f"{n_chunks / elapsed:.0f} chunks/s · {elapsed / 60:.1f} min", flush=True)

        index_keys(conn, todo, progress)
        # a running triage worker won't see these until its BM25 index is rebuilt
        conn.execute("update index_state set bm25_version = bm25_version + 1 where id = 1")


if __name__ == "__main__":
    main()
