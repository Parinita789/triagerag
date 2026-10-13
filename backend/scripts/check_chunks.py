import random
from collections import Counter

import psycopg

from triagerag.config import settings
from triagerag.index.chunk import chunk_ticket
from triagerag.shared.clean import clean_ticket


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute("select raw from tickets").fetchall()

    all_chunks = []
    for (issue,) in rows:
        all_chunks.extend(chunk_ticket(clean_ticket(issue)))

    sections = Counter(c.section for c in all_chunks)
    words = sorted(len(c.content.split()) for c in all_chunks)
    with_templates = sum(1 for c in all_chunks if c.template_ids)
    with_exc = sum(1 for c in all_chunks if c.exceptions)

    print(f"tickets: {len(rows):,}   chunks: {len(all_chunks):,}   per ticket: {len(all_chunks) / len(rows):.1f}")
    print(f"sections: {dict(sections)}")
    print(f"words per chunk: median {words[len(words) // 2]}, p95 {words[int(len(words) * .95)]}, max {words[-1]}")
    print(f"chunks with template ids: {with_templates:,}   with exceptions: {with_exc:,}")

    print("\n--- 3 random chunks ---")
    for c in random.Random(1).sample(all_chunks, 3):
        print(f"\n[{c.section}] templates={c.template_ids} exceptions={c.exceptions}")
        print(c.content[:600])


if __name__ == "__main__":
    main()