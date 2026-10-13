"""Indexing jobs: one ticket (from a 'resolved' webhook) or the whole corpus (fan-out)."""
from collections import Counter
from collections.abc import Callable

import httpx
import psycopg
from pgvector.psycopg import register_vector

from triagerag.index.chunk import MAX_WORDS, OVERLAP_WORDS, chunk_ticket
from triagerag.index.embed import MODEL_NAME, embed_documents
from triagerag.index.jira import FIELDS, _link_rows, _ticket_row
from triagerag.jobs.events import step
from triagerag.jobs.registry import job
from triagerag.shared.clean import redact_ticket, structure_ticket

ISSUE_URL = "https://issues.apache.org/jira/rest/api/2/issue/{key}"
TICKETS_PER_BATCH = 200
BM25_REFRESH_DELAY = 5  # seconds: lets a burst of index jobs share one refresh
INSERT = """insert into chunks (ticket_key, section, content,
                                template_ids, exception_signatures, embedding, created_at)
            values (%s, %s, %s, %s, %s, %s, %s)"""


def fetch_issue(key: str) -> dict:
    r = httpx.get(ISSUE_URL.format(key=key), params={"fields": FIELDS}, timeout=60)
    r.raise_for_status()
    return r.json()


def upsert_ticket(conn: psycopg.Connection, issue: dict) -> None:
    """Insert or refresh one ticket. New tickets join the 'index' split; an existing split is kept."""
    with conn.transaction():
        conn.execute(
            """insert into tickets (key, summary, description, status, resolution,
                                    issue_type, components, created_at, resolved_at, raw, split)
               values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'index')
               on conflict (key) do update set
                 summary = excluded.summary, description = excluded.description,
                 status = excluded.status, resolution = excluded.resolution,
                 issue_type = excluded.issue_type, components = excluded.components,
                 resolved_at = excluded.resolved_at, raw = excluded.raw""",
            _ticket_row(issue))
        for row in _link_rows(issue):
            conn.execute("""insert into ticket_links (from_key, to_key, link_type) values (%s, %s, %s)
                            on conflict do nothing""", row)


def index_keys(conn: psycopg.Connection, keys: list[str],
               progress: Callable[[int, int], None] | None = None) -> int:
    """load → clean → redact → chunk → embed → store, per batch of TICKETS_PER_BATCH tickets, each a
    recorded step inside a job. Idempotent: a batch's old chunks are deleted and the new ones
    inserted in one transaction, so indexing twice never duplicates. Returns chunks written."""
    register_vector(conn)
    n_chunks = 0
    for i in range(0, len(keys), TICKETS_PER_BATCH):
        batch = keys[i:i + TICKETS_PER_BATCH]
        n_chunks += _index_batch(conn, batch)
        if progress:
            progress(i + len(batch), n_chunks)
    return n_chunks


def _index_batch(conn: psycopg.Connection, batch: list[str]) -> int:
    with step("load", {"tickets": len(batch)}) as st:
        raws = [r[0] for r in conn.execute("select raw from tickets where key = any(%s)", (batch,))]
        st.output = {"tickets": len(raws), "keys": batch[:20]}

    with step("clean") as st:
        structured = [structure_ticket(raw) for raw in raws]
        first = structured[0] if structured else None
        st.output = {
            "tickets": len(structured),
            "comments_kept": sum(len(t.comments) for t in structured),
            "bot_comments_dropped": sum(t.bot_comments_dropped for t in structured),
            "example": first and {"key": first.key, "prose": first.description.prose[:600],
                                  "code_blocks": len(first.description.blocks)},
            "what": "Jira markup stripped, code blocks split from prose, bot comments dropped"}

    with step("redact") as st:
        tickets = [redact_ticket(t) for t in structured]
        counts = sum((t.redactions for t in tickets), Counter())
        st.output = {"counts": dict(counts),
                     "tickets_with_redactions": sum(1 for t in tickets if t.redactions)}

    with step("chunk", {"max_words": MAX_WORDS, "overlap_words": OVERLAP_WORDS}) as st:
        chunks = [c for t in tickets for c in chunk_ticket(t)]
        words = [len(c.content.split()) for c in chunks]
        st.output = {
            "chunks": len(chunks), "by_section": dict(Counter(c.section for c in chunks)),
            "avg_words": round(sum(words) / len(words)) if words else 0,
            "examples": [{"ticket": c.ticket_key, "section": c.section, "content": c.content[:500],
                          "template_ids": c.template_ids, "exceptions": c.exceptions}
                         for c in chunks[:3]]}

    with step("embed", {"model": MODEL_NAME}) as st:
        vectors = embed_documents([c.content for c in chunks], show_progress=False) if chunks else []
        st.output = {"vectors": len(vectors), "dims": len(vectors[0]) if len(vectors) else 0}

    with step("store") as st, conn.transaction():
        deleted = conn.execute("delete from chunks where ticket_key = any(%s)", (batch,)).rowcount
        with conn.cursor() as cur:
            cur.executemany(INSERT, [
                (c.ticket_key, c.section, c.content, c.template_ids, c.exceptions, v, c.created_at)
                for c, v in zip(chunks, vectors)])
        st.output = {"deleted_old_chunks": deleted, "inserted": len(chunks),
                     "what": "delete + insert in one transaction: re-indexing never duplicates"}
    return len(chunks)


def unindexed_keys(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        """select key from tickets t
           where not exists (select 1 from chunks c where c.ticket_key = t.key)
           order by key""")]


def _refresh_bm25(ctx) -> None:
    with step("refresh_bm25") as st:
        job_id = ctx.enqueue("refresh_bm25", dedupe_key="bm25", delay=BM25_REFRESH_DELAY)
        st.output = {"job": job_id, "in_seconds": BM25_REFRESH_DELAY,
                     "what": "deduped: a burst of index jobs shares one refresh; triage workers "
                             "rebuild their in-memory BM25 when the version changes"}


INDEX_STAGES = ("load", "clean", "redact", "chunk", "embed", "store", "refresh_bm25")


@job("index_ticket", stages=("fetch", *INDEX_STAGES))
def index_ticket(ctx, payload: dict) -> dict:
    key = payload["key"]
    with step("fetch", {"url": ISSUE_URL.format(key=key)}) as st:
        issue = fetch_issue(key)
        upsert_ticket(ctx.conn, issue)
        f = issue["fields"]
        st.output = {"key": key, "summary": f.get("summary"),
                     "comments": len((f.get("comment") or {}).get("comments", [])),
                     "resolution": (f.get("resolution") or {}).get("name")}
    n = index_keys(ctx.conn, [key])
    _refresh_bm25(ctx)
    return {"key": key, "chunks": n}


@job("reindex_all", max_attempts=1, stages=("select", "fan_out"))
def reindex_all(ctx, payload: dict) -> dict:
    """Fan-out: one index_batch child per TICKETS_PER_BATCH tickets. Progress = finished children / total.
    rebuild=true re-indexes every ticket (each batch replaces its own chunks); otherwise only the
    tickets that have no chunks yet."""
    with step("select", {"rebuild": bool(payload.get("rebuild")), "limit": payload.get("limit")}) as st:
        if payload.get("rebuild"):
            keys = [r[0] for r in ctx.conn.execute("select key from tickets order by key")]
        else:
            keys = unindexed_keys(ctx.conn)
        if payload.get("limit"):
            keys = keys[:payload["limit"]]
        st.output = {"tickets": len(keys)}
    with step("fan_out", {"per_child": TICKETS_PER_BATCH}) as st:
        children = [ctx.enqueue("index_batch", {"keys": keys[i:i + TICKETS_PER_BATCH]},
                                parent_id=ctx.job.id)
                    for i in range(0, len(keys), TICKETS_PER_BATCH)]
        st.output = {"children": children}
    return {"tickets": len(keys), "children": len(children)}


@job("index_batch", stages=INDEX_STAGES)
def index_batch(ctx, payload: dict) -> dict:
    keys = payload["keys"]
    n = index_keys(ctx.conn, keys, progress=lambda *_: ctx.heartbeat())
    _refresh_bm25(ctx)  # deduped: the batches of one reindex share a few refreshes, not one each
    return {"tickets": len(keys), "chunks": n}
