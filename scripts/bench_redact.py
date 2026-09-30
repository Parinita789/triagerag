import time
from collections import Counter

import psycopg

from triagerag.config import settings
from triagerag.shared.redact import redact


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("select description from tickets where description is not null")
            descriptions = [row[0] for row in cur.fetchall()]

    total_bytes = 0
    found: Counter[str] = Counter()

    start = time.perf_counter()
    for text in descriptions:
        total_bytes += len(text.encode())
        _, counts = redact(text)
        found.update(counts)
    elapsed = time.perf_counter() - start

    mb = total_bytes / 1e6
    print(f"{len(descriptions):,} descriptions, {mb:.1f} MB in {elapsed:.2f}s → {mb / elapsed:.1f} MB/s")
    print("redacted:", dict(found))


if __name__ == "__main__":
    main()