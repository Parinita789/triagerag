from collections import defaultdict

from triagerag.eval.queries import EvalQuery


def evaluate(queries: list[EvalQuery], ranked: dict[str, list[str]], k: int = 5) -> dict[str, float]:
    """ranked: query key -> ticket keys, best first."""
    hits = recalls = rr = 0.0
    for q in queries:
        top = ranked.get(q.key, [])
        found = [t for t in top[:k] if t in q.relevant]
        hits += bool(found)
        recalls += len(found) / len(q.relevant)
        first = next((i for i, t in enumerate(top[:10], 1) if t in q.relevant), None)
        rr += 1 / first if first else 0
    n = len(queries)
    return {f"hit@{k}": hits / n, f"recall@{k}": recalls / n, "mrr@10": rr / n}


def recall_by_link_type(queries: list[EvalQuery], ranked: dict[str, list[str]], k: int = 5) -> dict[str, str]:
    found: dict[str, int] = defaultdict(int)
    total: dict[str, int] = defaultdict(int)
    for q in queries:
        top = set(ranked.get(q.key, [])[:k])
        for ticket, link_type in q.relevant.items():
            total[link_type] += 1
            found[link_type] += ticket in top
    return {t: f"{found[t]}/{total[t]} ({found[t] / total[t]:.0%})" for t in total}