import psycopg

from triagerag.config import settings
from triagerag.eval.queries import load_main_queries
from triagerag.shared.clean import clean_ticket
from triagerag.shared.extract import extract


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        queries = load_main_queries(conn)
        keys = [q.key for q in queries]
        raws = dict(conn.execute(
            "select key, raw from tickets where key = any(%s)", (keys,)).fetchall())

    n_tpl = n_exc = n_either = 0
    for q in queries:
        ex = extract(clean_ticket(raws[q.key]).description)
        has_tpl, has_exc = bool(ex.template_ids), bool(ex.exception_classes)
        n_tpl += has_tpl
        n_exc += has_exc
        n_either += has_tpl or has_exc

    n = len(queries)
    print(f"queries with template ids:   {n_tpl}/{n}")
    print(f"queries with exceptions:     {n_exc}/{n}")
    print(f"queries with either:         {n_either}/{n}")


if __name__ == "__main__":
    main()