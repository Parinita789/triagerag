from collections import defaultdict

RRF_K = 60


def rrf(rankings: list[list[str]], k: int = RRF_K, top: int = 10) -> list[str]:
    scores: dict[str, float] = defaultdict(float)
    for ranking in rankings:
        for rank, ticket in enumerate(ranking, start=1):
            scores[ticket] += 1 / (k + rank)
    return sorted(scores, key=scores.__getitem__, reverse=True)[:top]