"""Worker: requeue stale → claim → run handler → complete/fail, in a loop.

    python -m triagerag.jobs.worker --types triage_ticket
    python -m triagerag.jobs.worker --types index_ticket,index_batch,reindex_all,refresh_bm25,load_logs

Run as many as you like: FOR UPDATE SKIP LOCKED gives each job to exactly one of them.
SIGTERM/Ctrl-C finishes the current job, then exits."""
import argparse
import signal
import time
import traceback
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any

import psycopg

from triagerag.config import settings
from triagerag.jobs import queue, registry
from triagerag.jobs.events import current_attempt, current_job
from triagerag.jobs.queue import Job

POLL_SECONDS = 1.0


class Resources:
    """Heavy things built once per process, and only when a handler first asks for them."""

    def __init__(self) -> None:
        self.bm25_version = -1

    @cached_property
    def search_conn(self) -> psycopg.Connection:
        return psycopg.connect(settings.database_url, autocommit=True)

    @cached_property
    def search(self) -> Any:
        from triagerag.index.embed import model
        from triagerag.query.rerank import reranker
        from triagerag.query.search import SearchService
        service = SearchService(self.search_conn)
        self.bm25_version = current_bm25_version(self.search_conn)
        model()  # load the embedder now, not inside the first job's 'embed' step
        if service.preview_rerank:
            reranker()  # same for the cross-encoder (~1 GB): the first 'rerank' step would take seconds
        return service

    @cached_property
    def llm(self) -> Any:
        from triagerag.query.llm import LLMClient
        return LLMClient(settings.openrouter_api_key, settings.openrouter_model)


def current_bm25_version(conn: psycopg.Connection) -> int:
    row = conn.execute("select bm25_version from index_state where id = 1").fetchone()
    return row[0] if row else 0


@dataclass
class JobContext:
    job: Job
    conn: psycopg.Connection
    resources: Resources = field(default_factory=Resources)

    def heartbeat(self) -> None:
        queue.heartbeat(self.conn, self.job.id)

    def enqueue(self, type: str, payload: dict | None = None, **opts: Any) -> int:
        return queue.enqueue(self.conn, type, payload, **opts)


def run_one(conn: psycopg.Connection, job: Job, resources: Resources) -> str:
    """Run a claimed job to completion or failure. Returns the outcome for the log line."""
    token = current_job.set(job.id)
    attempt_token = current_attempt.set(job.attempts)
    try:
        result = registry.get(job.type).fn(JobContext(job, conn, resources), job.payload)
        queue.complete(conn, job.id, result)
        return "done"
    except Exception as e:  # never let one bad job kill the worker
        tb = traceback.format_exc(limit=5)
        return queue.fail(conn, job.id, f"{type(e).__name__}: {e}\n\n{tb}")
    finally:
        current_job.reset(token)
        current_attempt.reset(attempt_token)


def work(conn: psycopg.Connection, types: list[str], resources: Resources,
         once: bool = False, stop: list[bool] | None = None) -> None:
    stop = stop if stop is not None else [False]
    while not stop[0]:
        queue.requeue_stale(conn)
        job = queue.claim(conn, types)
        if job is None:
            if once:
                return
            time.sleep(POLL_SECONDS)
            continue
        start = time.perf_counter()
        outcome = run_one(conn, job, resources)
        ms = int((time.perf_counter() - start) * 1000)
        print(f"job type={job.type} id={job.id} attempt={job.attempts}/{job.max_attempts} "
              f"ms={ms} outcome={outcome}", flush=True)
        if once:
            return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--types", required=True, help="comma-separated job types to run")
    ap.add_argument("--once", action="store_true", help="run one job (or none) and exit")
    args = ap.parse_args()
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    for t in types:
        registry.get(t)  # fail at startup on a typo, not silently never claiming anything

    stop = [False]

    def on_signal(signum: int, _frame: object) -> None:
        print(f"signal {signum}: finishing the current job, then exiting", flush=True)
        stop[0] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    with psycopg.connect(settings.database_url, autocommit=True) as conn:
        resources = Resources()
        if "triage_ticket" in types:
            print("loading search: BM25 index, embedder, reranker...", flush=True)
            resources.search  # noqa: B018 - build before the first job instead of during it
        print(f"worker ready: {', '.join(types)}", flush=True)
        work(conn, types, resources, once=args.once, stop=stop)


if __name__ == "__main__":
    main()
