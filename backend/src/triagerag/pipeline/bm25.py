"""The BM25 index lives in each triage worker's memory, so another process can't update it.
refresh_bm25 bumps a shared version number; triage workers rebuild when theirs is behind."""
from triagerag.jobs.events import step
from triagerag.jobs.registry import job


@job("refresh_bm25", stages=("bump_version",))
def refresh_bm25(ctx, payload: dict) -> dict:
    with step("bump_version") as st:
        version = ctx.conn.execute(
            "update index_state set bm25_version = bm25_version + 1 where id = 1 returning bm25_version"
        ).fetchone()[0]
        st.output = {"bm25_version": version,
                     "what": "triage workers compare this with their own and rebuild before the next triage"}
    return {"bm25_version": version}
