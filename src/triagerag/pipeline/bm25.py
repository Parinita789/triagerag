"""The BM25 index lives in each triage worker's memory, so another process can't update it.
refresh_bm25 bumps a shared version number; triage workers rebuild when theirs is behind."""
from triagerag.jobs.registry import job


@job("refresh_bm25")
def refresh_bm25(ctx, payload: dict) -> dict:
    version = ctx.conn.execute(
        "update index_state set bm25_version = bm25_version + 1 where id = 1 returning bm25_version"
    ).fetchone()[0]
    return {"bm25_version": version}
