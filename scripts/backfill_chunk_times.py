import psycopg

from triagerag.config import settings
from triagerag.index.chunk import chunk_ticket
from triagerag.shared.clean import clean_ticket


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        keys = [r[0] for r in conn.execute("select key from tickets order by key")]
        updated = 0
        for i in range(0, len(keys), 500):
            batch = keys[i:i + 500]
            raws = conn.execute("select raw from tickets where key = any(%s)", (batch,)).fetchall()
            rows = [(c.created_at, c.ticket_key, c.content)
                    for (raw,) in raws for c in chunk_ticket(clean_ticket(raw))]
            with conn.cursor() as cur:
                cur.executemany(
                    "update chunks set created_at = %s where ticket_key = %s and content = %s", rows)
                updated += cur.rowcount
            conn.commit()
            print(f"  {i + len(batch):,}/{len(keys):,} tickets")

        missing = conn.execute("select count(*) from chunks where created_at is null").fetchone()[0]
        print(f"chunks still missing created_at: {missing}")


if __name__ == "__main__":
    main()