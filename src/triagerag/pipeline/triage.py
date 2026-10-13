"""triage_ticket: retrieval first, then one LLM call, then the comment that would be posted to Jira."""
import time
from collections import Counter
from datetime import datetime, timezone

from psycopg.types.json import Jsonb

from triagerag.eval.queries import TICKET_KEY_RE
from triagerag.index.chunk import render
from triagerag.jobs.events import record_step, step
from triagerag.jobs.registry import job
from triagerag.jobs.worker import current_bm25_version
from triagerag.query.agent import AgentResult, run_agent
from triagerag.query.BM25 import BM25Index
from triagerag.query.tools import ToolContext
from triagerag.shared.clean import redact_clean_text, structure_text
from triagerag.shared.extract import extract
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
        start = time.perf_counter()
        service.bm25 = BM25Index(resources.search_conn)
        record_step("bm25.sync", output={"rebuilt": True, "version": version,
                                          "chunks": len(service.bm25.tickets)},
                    ms=int((time.perf_counter() - start) * 1000))
        resources.bm25_version = version


STAGES = ("clean", "redact", "extract", "embed", "dense", "bm25", "hybrid", "rerank",
          "llm", "validate", "comment")


def prepare(summary: str, description: str, prior_counts: dict | None = None) -> str:
    """clean → redact → extract, each a recorded step. Returns the query text, built exactly like
    the eval queries (eval/queries.py: summary + rendered description, ticket keys removed), so the
    live pipeline is the one that was measured."""
    with step("clean", {"summary": summary, "description": description}) as st:
        structured = structure_text(description)
        summary = " ".join(summary.split())
        st.output = {"summary": summary, "prose": structured.prose, "code_blocks": structured.blocks,
                     "what": "Jira markup stripped, code/noformat blocks split from prose"}

    with step("redact") as st:
        summary, c1 = redact(summary)
        text, c2 = redact_clean_text(structured)
        counts = Counter(prior_counts or {}) + c1 + c2  # prior: redacted at enqueue (webhook / API)
        st.output = {"counts": dict(counts), "summary": summary, "text": render(text),
                     "what": "secrets and PII replaced with keyed pseudonyms; values never stored"}

    with step("extract") as st:
        ex = extract(text)
        st.output = {"log_lines": [{"message": line.message, "template_id": line.template_id}
                                   for line in ex.logs[:20]],
                     "template_ids": sorted(ex.template_ids), "exceptions": sorted(ex.exception_classes),
                     "what": "log lines and exceptions found by shape, matched to Drain templates"}

    return TICKET_KEY_RE.sub("", f"{summary}\n\n{render(text)}".strip())


@job("triage_ticket", stages=STAGES)
def triage_ticket(ctx, payload: dict) -> dict:
    """Payload: key, summary, description (already redacted at enqueue), created_at (ISO).
    Without a key ("Try it" in the dashboard), the triage is stored as TRY-<job id>."""
    key = payload.get("key") or f"TRY-{ctx.job.id}"
    query = prepare(payload.get("summary") or "", payload.get("description") or "",
                    payload.get("redactions"))
    summary, _, description = query.partition("\n\n")
    created_at = parse_time(payload.get("created_at"))

    sync_bm25(ctx.resources)
    start = time.time()
    tctx = ToolContext(service=ctx.resources.search, before=created_at, exclude=key)
    r = run_agent(query, tctx, ctx.resources.llm, extra_calls=0)
    with step("comment") as st:
        # written even when the LLM failed: then it lists the related tickets
        comment = format_comment(r)
        st.output = {"comment": comment, "stopped": r.stopped, "posted": False,
                     "what": "dry run: stored in triages.comment, not posted to Jira"}
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
