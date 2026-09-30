import sys

import psycopg

from triagerag.config import settings
from triagerag.shared.clean import clean_ticket

DEFAULT_KEYS = ["HDFS-17980", "HDFS-6536", "HDFS-162"]


def main() -> None:
    keys = sys.argv[1:] or DEFAULT_KEYS
    with psycopg.connect(settings.database_url) as conn:
        for key in keys:
            row = conn.execute("select raw from tickets where key = %s", (key,)).fetchone()
            if not row:
                print(f"{key}: not found")
                continue
            issue = row[0]
            t = clean_ticket(issue)

            print("=" * 100)
            print(f"{t.key}: {t.summary}")
            print("\n--- ORIGINAL DESCRIPTION ---")
            print((issue["fields"].get("description") or "")[:1500])
            print("\n--- CLEANED PROSE ---")
            print(t.description.prose[:1500])
            for i, block in enumerate(t.description.blocks, 1):
                print(f"\n--- BLOCK {i} ---")
                print(block[:800])
            print(f"\ncomments kept: {len(t.comments)}, bots dropped: {t.bot_comments_dropped}")
            print(f"redactions: {dict(t.redactions)}")


if __name__ == "__main__":
    main()