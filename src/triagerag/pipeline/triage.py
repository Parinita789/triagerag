"""triage_ticket: retrieval first, then one LLM call, then the comment that would be posted to Jira."""
import time
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from triagerag.jobs.registry import job
from triagerag.jobs.worker import current_bm25_version
from triagerag.query.agent import AgentResult, run_agent
from triagerag.query.BM25 import BM25Index
from triagerag.query.tools import ToolContext
from triagerag.shared.redact import redact

BROWSE = "https://issues.apache.org/jira/browse/"


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


def parse_time(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat(value) if value else datetime.now(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def sync_bm25(resources) -> None:
    """Rebuild this process's BM25 index if refresh_bm25 ran since it was built.
    The version is read before rebuilding, so a refresh that lands mid-rebuild isn't missed."""
    service = resources.search
    version = current_bm25_version(resources.search_conn)
    if version != resources.bm25_version:
        service.bm25 = BM25Index(resources.search_conn)
        resources.bm25_version = version


@job("triage_ticket")
def triage_ticket(ctx, payload: dict) -> dict:
    """Payload: key, summary, description (already redacted at enqueue), created_at (ISO).
    Without a key ("Try it" in the dashboard), the triage is stored as TRY-<job id>."""
    key = payload.get("key") or f"TRY-{ctx.job.id}"
    summary = redact(payload.get("summary") or "")[0]
    description = redact(payload.get("description") or "")[0]
    created_at = parse_time(payload.get("created_at"))

    sync_bm25(ctx.resources)
    start = time.time()
    tctx = ToolContext(service=ctx.resources.search, before=created_at, exclude=key)
    r = run_agent(f"{summary}\n\n{description}".strip(), tctx, ctx.resources.llm, extra_calls=0)
    comment = format_comment(r)  # written even when the LLM failed: then it lists the related tickets
    result = {
        "key": key, "stopped": r.stopped, "related": r.related, "llm_calls": r.llm_calls,
        "tokens": r.tokens, "seconds": round(time.time() - start, 1), "error": r.error,
        "diagnosis": r.diagnosis.model_dump() if r.diagnosis else None,
    }
    ctx.conn.execute(
        """insert into triages (issue_key, job_id, summary, description, created_at,
                                finished_at, comment, result)
           values (%s, %s, %s, %s, %s, now(), %s, %s)
           on conflict (issue_key) do update set
             job_id = excluded.job_id, finished_at = now(),
             comment = excluded.comment, result = excluded.result""",
        (key, ctx.job.id, summary, description, created_at, comment, Jsonb(result)))
    return result
