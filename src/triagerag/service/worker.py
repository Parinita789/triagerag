"""Worker: claim queued triage jobs, run retrieval + LLM, store the comment.
Safe to run several at once: FOR UPDATE SKIP LOCKED gives each job to exactly one worker."""
import argparse
import time

import psycopg
from psycopg.types.json import Json

from triagerag.config import settings
from triagerag.query.agent import AgentResult, run_agent
from triagerag.query.llm import LLMClient
from triagerag.query.search import SearchService
from triagerag.query.tools import ToolContext
from triagerag.shared.redact import redact

MAX_ATTEMPTS = 3
STALE_MINUTES = 10
POLL_SECONDS = 2
BROWSE = "https://issues.apache.org/jira/browse/"

CLAIM = """
update triage_jobs
set status = 'running', attempts = attempts + 1, started_at = now(), error = null
where issue_key = (
    select issue_key from triage_jobs
    where status = 'queued' and attempts < %s
    order by received_at
    for update skip locked
    limit 1)
returning issue_key, summary, description, created_at"""

# a worker that died mid-job leaves it 'running' forever; give it back (or give up after MAX_ATTEMPTS)
REQUEUE_STALE = """
update triage_jobs
set status = case when attempts >= %s then 'failed' else 'queued' end,
    error = coalesce(error, 'worker stopped mid-job')
where status = 'running' and started_at < now() - make_interval(mins => %s)"""


def _related(keys: list[str]) -> list[str]:
    return [f"- {k}: {BROWSE}{k}" for k in keys] or ["- none found"]


def format_comment(r: AgentResult) -> str:
    d = r.diagnosis
    if d is None:
        lines = [f"Automated triage couldn't write a diagnosis ({r.stopped}). "
                 "Possibly related past tickets:", *_related(r.related)]
    elif d.abstain:
        lines = ["Automated triage: not enough evidence for a diagnosis.",
                 f"Why: {d.abstain_reason or d.likely_cause}", "",
                 "Possibly related past tickets:", *_related(r.related)]
    else:
        lines = [f"Automated triage (confidence: {d.confidence})", "",
                 f"Summary: {d.summary}", f"Likely cause: {d.likely_cause}", "",
                 "Related past tickets:",
                 *[f"- {t.key}: {t.why} ({BROWSE}{t.key})" for t in d.related_tickets]]
        if d.next_steps:
            lines += ["", "Next steps:", *[f"- {s}" for s in d.next_steps]]
    lines += ["", "Generated automatically and may be wrong. Check the cited tickets before acting on it."]
    return redact("\n".join(lines))[0]  # the LLM can still produce something a detector should catch


def process(job: tuple, service: SearchService, llm: LLMClient, jobs: psycopg.Connection) -> str:
    key, summary, description, created_at = job
    start = time.time()
    ctx = ToolContext(service=service, before=created_at, exclude=key)
    r = run_agent(f"{summary}\n\n{description}".strip(), ctx, llm, extra_calls=0)
    result = {
        "stopped": r.stopped, "related": r.related, "llm_calls": r.llm_calls, "tokens": r.tokens,
        "seconds": round(time.time() - start, 1), "error": r.error,
        "diagnosis": r.diagnosis.model_dump() if r.diagnosis else None,
    }
    jobs.execute("update triage_jobs set status = 'done', finished_at = now(), comment = %s, result = %s "
                 "where issue_key = %s", (format_comment(r), Json(result), key))
    return r.stopped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="process one job (or none) and exit")
    args = ap.parse_args()

    # autocommit: a claim must be visible to other workers immediately, not at the end of a transaction
    with psycopg.connect(settings.database_url, autocommit=True) as jobs, \
         psycopg.connect(settings.database_url, autocommit=True) as search_conn:
        service = SearchService(search_conn)  # builds the BM25 index once, at startup
        llm = LLMClient(settings.openrouter_api_key, settings.openrouter_model)
        print("worker ready", flush=True)

        while True:
            jobs.execute(REQUEUE_STALE, (MAX_ATTEMPTS, STALE_MINUTES))
            job = jobs.execute(CLAIM, (MAX_ATTEMPTS,)).fetchone()
            if job is None:
                if args.once:
                    print("no queued jobs")
                    return
                time.sleep(POLL_SECONDS)
                continue

            print(f"claimed {job[0]} (attempt)", flush=True)
            try:
                stopped = process(job, service, llm, jobs)
                print(f"done {job[0]}: {stopped}", flush=True)
            except Exception as e:  # never let one bad job kill the worker
                jobs.execute("update triage_jobs set status = case when attempts >= %s then 'failed' "
                             "else 'queued' end, error = %s where issue_key = %s",
                             (MAX_ATTEMPTS, f"{type(e).__name__}: {e}"[:500], job[0]))
                print(f"error {job[0]}: {type(e).__name__}: {e}", flush=True)
            if args.once:
                return


if __name__ == "__main__":
    main()