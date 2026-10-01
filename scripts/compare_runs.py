import json
import sys
from math import comb
from pathlib import Path

import psycopg

from triagerag.config import settings
from triagerag.eval.queries import load_set

K = 5


def hits(ranked: dict[str, list[str]], queries) -> set[str]:
    return {q.key for q in queries if any(t in q.relevant for t in ranked.get(q.key, [])[:K])}


def main() -> None:
    set_name, a_name, b_name = sys.argv[1], sys.argv[2], sys.argv[3]
    a = json.loads(Path(f"data/eval/{set_name}_{a_name}.json").read_text())
    b = json.loads(Path(f"data/eval/{set_name}_{b_name}.json").read_text())
    with psycopg.connect(settings.database_url) as conn:
        queries = load_set(conn, set_name)

    ha, hb = hits(a, queries), hits(b, queries)
    n = len(queries)
    print(f"[{set_name}] {n} queries")
    print(f"hit@{K} by {a_name} only: {len(ha - hb)}")
    print(f"hit@{K} by {b_name} only: {len(hb - ha)}")
    print(f"hit@{K} by both:          {len(ha & hb)}")
    print(f"hit@{K} by neither:       {n - len(ha | hb)}")
    print(f"\nceiling if fusion were perfect: {len(ha | hb)}/{n} = {len(ha | hb) / n:.3f}")

    only_a, only_b = len(ha - hb), len(hb - ha)
    n_disc = only_a + only_b
    if n_disc:
        k = max(only_a, only_b)
        p = min(1.0, 2 * sum(comb(n_disc, i) for i in range(k, n_disc + 1)) / 2 ** n_disc)
        print(f"sign test on {n_disc} disagreements: p = {p:.3f}"
              f"{'  (significant)' if p < 0.05 else '  (not significant at 0.05)'}")


if __name__ == "__main__":
    main()