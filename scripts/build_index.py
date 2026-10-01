import argparse
import time

import psycopg
import torch
from pgvector.psycopg import register_vector

from triagerag.config import settings
from triagerag.index.chunk import chunk_ticket
from triagerag.index.embed import embed_documents, model
from triagerag.shared.clean import clean_ticket

TICKETS_PER_BATCH = 200
INSERT = """insert into chunks (ticket_key, section, content,
                                template_ids, exception_signatures, embedding, created_at)
            values (%s, %s, %s, %s, %s, %s, %s)"""


def main() -> None:
    ap = argparse.ArgumentParser(description="Chunk, embed, and store tickets. Resumable.")
    ap.add_argument("--rebuild", action="store_true", help="delete all chunks and start over")
    ap.add_argument("--limit", type=int, help="only process this many tickets (for timing)")
    ap.add_argument("--threads", type=int, default=4, help="CPU threads for the model")
    args = ap.parse_args()

    torch.set_num_threads(args.threads)
    print("model device:", model().device)

    with psycopg.connect(settings.database_url) as conn:
        register_vector(conn)
        if args.rebuild:
            conn.execute("truncate chunks")
            conn.commit()

        done = {r[0] for r in conn.execute("select distinct ticket_key from chunks")}
        keys = [r[0] for r in conn.execute("select key from tickets order by key")]
        todo = [k for k in keys if k not in done]
        if args.limit:
            todo = todo[:args.limit]
        print(f"{len(done):,} tickets already indexed, {len(todo):,} to go")

        start = time.perf_counter()
        n_chunks = 0
        for i in range(0, len(todo), TICKETS_PER_BATCH):
            batch_keys = todo[i:i + TICKETS_PER_BATCH]
            raws = conn.execute("select raw from tickets where key = any(%s)", (batch_keys,)).fetchall()
            chunks = [c for (raw,) in raws for c in chunk_ticket(clean_ticket(raw))]
            vectors = embed_documents([c.content for c in chunks], show_progress=False)

            with conn.cursor() as cur:
                cur.executemany(INSERT, [
                    (c.ticket_key, c.section, c.content, c.template_ids, c.exceptions, v, c.created_at)
                    for c, v in zip(chunks, vectors)
                ])
            conn.commit()

            n_chunks += len(chunks)
            elapsed = time.perf_counter() - start
            print(f"  {i + len(batch_keys):,}/{len(todo):,} tickets · {n_chunks:,} chunks · "
                  f"{n_chunks / elapsed:.0f} chunks/s · {elapsed / 60:.1f} min")


if __name__ == "__main__":
    main()