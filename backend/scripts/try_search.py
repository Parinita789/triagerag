import sys

import psycopg

from triagerag.config import settings
from triagerag.eval.queries import load_dev_queries
from triagerag.query.search import SearchService


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        queries = load_dev_queries(conn)
        q = next(q for q in queries if q.key == sys.argv[1]) if len(sys.argv) > 1 else queries[0]
        service = SearchService(conn)

        print(f"QUERY {q.key} (filed {q.created_at:%Y-%m-%d})")
        print(q.text[:300], "...\n")
        print(f"linked tickets (the answer): {list(q.relevant)}\n")

        for hit in service.search(q.text, q.created_at, q.key):
            mark = "✅" if hit.key in q.relevant else "  "
            print(f"{mark} {hit.key} [{hit.issue_type}, {hit.resolution}] {hit.summary}")
            print(f"     {hit.snippet[:200]!r}\n")

        first = service.search(q.text, q.created_at, q.key)[0].key
        detail = service.get_ticket(first, q.created_at, q.key)
        print(f"get_ticket({first}): {len(detail.comments)} comments, description {len(detail.description)} chars")
        print(f"get_ticket on the query itself → {service.get_ticket(q.key, q.created_at, q.key)}")


if __name__ == "__main__":
    main()