"""Indexing jobs: one ticket (from a 'resolved' webhook) or the whole corpus (fan-out)."""
from collections.abc import Callable

import httpx
import psycopg
from pgvector.psycopg import register_vector

from triagerag.index.chunk import chunk_ticket
from triagerag.index.embed import embed_documents
from triagerag.index.jira import FIELDS, _link_rows, _ticket_row
from triagerag.jobs.registry import job
from triagerag.shared.clean import clean_ticket

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
    """Chunk, embed and store these tickets. Idempotent: each batch's old chunks are deleted and
    the new ones inserted in one transaction, so indexing twice never duplicates.
    Returns the number of chunks written."""
    register_vector(conn)
    n_chunks = 0
    for i in range(0, len(keys), TICKETS_PER_BATCH):
        batch = keys[i:i + TICKETS_PER_BATCH]
        raws = conn.execute("select raw from tickets where key = any(%s)", (batch,)).fetchall()
        chunks = [c for (raw,) in raws for c in chunk_ticket(clean_ticket(raw))]
        vectors = embed_documents([c.content for c in chunks], show_progress=False) if chunks else []
        with conn.transaction():
            conn.execute("delete from chunks where ticket_key = any(%s)", (batch,))
            with conn.cursor() as cur:
                cur.executemany(INSERT, [
                    (c.ticket_key, c.section, c.content, c.template_ids, c.exceptions, v, c.created_at)
                    for c, v in zip(chunks, vectors)])
        n_chunks += len(chunks)
        if progress:
            progress(i + len(batch), n_chunks)
    return n_chunks


def unindexed_keys(conn: psycopg.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        """select key from tickets t
           where not exists (select 1 from chunks c where c.ticket_key = t.key)
           order by key""")]


def _refresh_bm25(ctx) -> None:
    ctx.enqueue("refresh_bm25", dedupe_key="bm25", delay=BM25_REFRESH_DELAY)


@job("index_ticket")
def index_ticket(ctx, payload: dict) -> dict:
    key = payload["key"]
    upsert_ticket(ctx.conn, fetch_issue(key))
    n = index_keys(ctx.conn, [key])
    _refresh_bm25(ctx)
    return {"key": key, "chunks": n}


@job("reindex_all", max_attempts=1)
def reindex_all(ctx, payload: dict) -> dict:
    """Fan-out: one index_batch child per TICKETS_PER_BATCH tickets. Progress = finished children / total.
    rebuild=true re-indexes every ticket (each batch replaces its own chunks); otherwise only the
    tickets that have no chunks yet."""
    if payload.get("rebuild"):
        keys = [r[0] for r in ctx.conn.execute("select key from tickets order by key")]
    else:
        keys = unindexed_keys(ctx.conn)
    if payload.get("limit"):
        keys = keys[:payload["limit"]]
    children = [ctx.enqueue("index_batch", {"keys": keys[i:i + TICKETS_PER_BATCH]}, parent_id=ctx.job.id)
                for i in range(0, len(keys), TICKETS_PER_BATCH)]
    return {"tickets": len(keys), "children": len(children)}


@job("index_batch")
def index_batch(ctx, payload: dict) -> dict:
    keys = payload["keys"]
    n = index_keys(ctx.conn, keys, progress=lambda *_: ctx.heartbeat())
    _refresh_bm25(ctx)  # deduped: the batches of one reindex share a few refreshes, not one each
    return {"tickets": len(keys), "chunks": n}
