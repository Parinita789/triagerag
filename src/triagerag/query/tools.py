import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, ValidationError, model_validator

from triagerag.query.logs import LogTools
from triagerag.query.search import SearchService

TICKET_KEY = r"^HDFS-\d+$"
TEMPLATE_REF = re.compile(r"\bT(\d+)\b")  # "T14 x3 | ..." in log tool output


class SearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    k: int = Field(default=5, ge=1, le=8)


class GetTicketArgs(BaseModel):
    key: str = Field(pattern=TICKET_KEY)


class LogArgs(BaseModel):
    start: str
    end: str
    block_id: str | None = None
    contains: str | None = None
    component: str | None = None
    level: str | None = None


class LogLinesArgs(LogArgs):
    limit: int = Field(default=20, ge=1, le=50)


class RelatedTicket(BaseModel):
    key: str = Field(pattern=TICKET_KEY)
    why: str


class EvidenceTemplate(BaseModel):
    template_id: int = Field(ge=1)
    why: str


class DiagnosisArgs(BaseModel):
    summary: str
    likely_cause: str
    related_tickets: list[RelatedTicket] = Field(default_factory=list)
    evidence_templates: list[EvidenceTemplate] = Field(default_factory=list)
    anomaly_found: bool | None = None  # log investigations only
    next_steps: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    abstain: bool
    abstain_reason: str = ""

    @model_validator(mode="after")
    def _cite_when_answering(self) -> "DiagnosisArgs":
        if self.abstain or self.anomaly_found is False:
            return self  # "nothing abnormal" needs no citations
        if not self.related_tickets and not self.evidence_templates:
            raise ValueError("no evidence cited: list what your diagnosis relies on (tickets in "
                             "related_tickets, log templates in evidence_templates), or set abstain=true")
        return self


_SEARCH = {"type": "function", "function": {
    "name": "search_past_tickets",
    "description": (
        "Search resolved HDFS Jira tickets for issues related to the new ticket. Combines meaning-based "
        "and keyword search. Returns up to k tickets, each with type, resolution, and the excerpt that "
        "matched best. Search several times with different wording if results look unrelated: try the "
        "symptom, the component (NameNode, DataNode, balancer, ...), and exact class or error names."),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "description": "What to search for, in plain words or exact error text"},
        "k": {"type": "integer", "minimum": 1, "maximum": 8, "description": "Number of tickets, default 5"},
    }, "required": ["query"]},
}}

_GET = {"type": "function", "function": {
    "name": "get_ticket",
    "description": (
        "Read one past ticket in more detail: its description and up to 6 comments. Use this before "
        "relying on a ticket from search results. Returns an error for tickets that don't exist or "
        "weren't resolved yet."),
    "parameters": {"type": "object", "properties": {
        "key": {"type": "string", "description": "Ticket key, e.g. HDFS-1234"},
    }, "required": ["key"]},
}}

_SUBMIT_TICKET = {"type": "function", "function": {
    "name": "submit_diagnosis",
    "description": (
        "Submit the final diagnosis. Call exactly once, as the last step. Only cite tickets returned by "
        "your searches or reads. If the evidence doesn't explain the failure, set abstain=true and say "
        "why: a confident wrong diagnosis is worse than none."),
    "parameters": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "One or two sentences: what is failing"},
        "likely_cause": {"type": "string", "description": "The most likely cause, based on the evidence"},
        "related_tickets": {
            "type": "array",
            "description": "Every past ticket your diagnosis relies on, with why it's relevant. "
                           "Required unless you abstain. Mentioning a ticket only in other text "
                           "doesn't count.",
            "items": {"type": "object", "properties": {
                "key": {"type": "string"}, "why": {"type": "string"}}, "required": ["key", "why"]}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "abstain": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
    }, "required": ["summary", "likely_cause", "related_tickets", "confidence", "abstain"]},
}}

_LOG_PARAMS = {
    "start": {"type": "string", "description": "ISO time, UTC, e.g. 2008-11-10T14:00:00Z"},
    "end": {"type": "string", "description": "ISO time, UTC. At most 6 hours after start"},
    "block_id": {"type": "string", "description": "Only lines for this block, e.g. blk_-1608999687919862906"},
    "contains": {"type": "string", "description": "Only lines containing this exact text"},
    "component": {"type": "string", "description": "Only this component (see loki_labels)"},
    "level": {"type": "string", "description": "Only this level: INFO or WARN"},
}

_TEMPLATE_STATS = {"type": "function", "function": {
    "name": "log_template_stats",
    "description": (
        "Summarize cluster logs as templates. Fetches matching lines from Loki (max 6h range, max 4000 "
        "lines) and groups them into templates like T14, rarest first, each with the share of blocks that "
        "normally contain it. RARE = in under 2% of blocks: uncommon, not necessarily wrong. With "
        "block_id, also lists expected write-path templates that are MISSING for that block."),
    "parameters": {"type": "object", "properties": _LOG_PARAMS, "required": ["start", "end"]},
}}

_LOG_LINES = {"type": "function", "function": {
    "name": "log_lines",
    "description": "Read up to 50 raw log lines (redacted), for context on a template. Same filters as "
                   "log_template_stats.",
    "parameters": {"type": "object", "properties": {
        **_LOG_PARAMS,
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20"},
    }, "required": ["start", "end"]},
}}

_LABELS = {"type": "function", "function": {
    "name": "loki_labels",
    "description": "List the components and levels you can filter on, and the time span the logs cover.",
    "parameters": {"type": "object", "properties": {}},
}}

_SUBMIT_LOG = {"type": "function", "function": {
    "name": "submit_diagnosis",
    "description": (
        "Submit your conclusion. Call exactly once, as the last step. anomaly_found=false (the block "
        "looks normal) is a valid answer. If anomaly_found=true, cite every template your conclusion "
        "relies on, including MISSING ones, using only template ids that appeared in your tool results."),
    "parameters": {"type": "object", "properties": {
        "summary": {"type": "string", "description": "One or two sentences: what the logs show"},
        "likely_cause": {"type": "string", "description": "Most likely cause, or why the block looks normal"},
        "anomaly_found": {"type": "boolean", "description": "Do the logs show something abnormal for this block?"},
        "evidence_templates": {
            "type": "array",
            "description": "Templates your conclusion relies on: the number after T (T14 -> 14) and why. "
                           "Required if anomaly_found is true.",
            "items": {"type": "object", "properties": {
                "template_id": {"type": "integer"}, "why": {"type": "string"}},
                "required": ["template_id", "why"]}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "abstain": {"type": "boolean"},
        "abstain_reason": {"type": "string"},
    }, "required": ["summary", "likely_cause", "anomaly_found", "evidence_templates", "confidence", "abstain"]},
}}

TICKET_TOOLS: list[dict[str, Any]] = [_SEARCH, _GET, _SUBMIT_TICKET]
LOG_TOOLS: list[dict[str, Any]] = [_TEMPLATE_STATS, _LOG_LINES, _LABELS, _SUBMIT_LOG]
TOOLS = TICKET_TOOLS  # kept for older imports


@dataclass
class TraceEvent:
    tool: str
    args: str
    result_chars: int
    ms: int
    error: str | None = None


@dataclass
class ToolContext:
    service: SearchService | None
    before: datetime | None   # the query ticket's creation time; never shown to the model
    exclude: str = ""         # the query ticket's own key
    logs: LogTools | None = None
    seen: set[str] = field(default_factory=set)
    seen_templates: set[int] = field(default_factory=set)
    last_search: list[str] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)


def _excerpt(snippet: str) -> str:
    body = snippet.split("\n\n", 1)[-1]          # drop the "[KEY] summary" header
    return re.sub(r"\s+", " ", body).strip()


def _need_search(ctx: ToolContext) -> SearchService:
    if ctx.service is None:
        raise LookupError("ticket search is not available here")
    return ctx.service


def _need_logs(ctx: ToolContext) -> LogTools:
    if ctx.logs is None:
        raise LookupError("log tools are not available here")
    return ctx.logs


def _run_search(args: SearchArgs, ctx: ToolContext) -> str:
    hits = _need_search(ctx).search(args.query, ctx.before, ctx.exclude, k=args.k)
    ctx.last_search = [h.key for h in hits]
    if not hits:
        return "No matching tickets."
    lines = []
    for i, h in enumerate(hits, 1):
        ctx.seen.add(h.key)
        when = f"{h.resolved_at:%Y-%m}" if h.resolved_at else "?"
        lines.append(f"{i}. {h.key} [{h.issue_type}, {h.resolution}, resolved {when}] {h.summary}\n"
                     f"   excerpt: {_excerpt(h.snippet)}")
    return "\n".join(lines)


def _run_get(args: GetTicketArgs, ctx: ToolContext) -> str:
    d = _need_search(ctx).get_ticket(args.key, ctx.before, ctx.exclude)
    if d is None:
        raise LookupError(f"{args.key} not found or not resolved yet")
    ctx.seen.add(d.key)
    parts = [f"{d.key} [{d.issue_type}, {d.resolution}] {d.summary}",
             f"DESCRIPTION: {_excerpt(d.description)}"]
    parts += [f"COMMENT {i}: {_excerpt(c)}" for i, c in enumerate(d.comments, 1)]
    return "\n\n".join(parts)


def _run_template_stats(args: LogArgs, ctx: ToolContext) -> str:
    out = _need_logs(ctx).template_stats(**args.model_dump())
    ctx.seen_templates.update(int(t) for t in TEMPLATE_REF.findall(out))
    return out


def _submit(parsed: dict, ctx: ToolContext) -> tuple[str, DiagnosisArgs]:
    d = DiagnosisArgs(**parsed)
    dropped = [t.key for t in d.related_tickets if t.key not in ctx.seen]
    dropped += [f"T{e.template_id}" for e in d.evidence_templates if e.template_id not in ctx.seen_templates]
    d.related_tickets = [t for t in d.related_tickets if t.key in ctx.seen]
    d.evidence_templates = [e for e in d.evidence_templates if e.template_id in ctx.seen_templates]
    # re-check: a diagnosis whose citations were all invented must not pass with an empty list
    d = DiagnosisArgs.model_validate(d.model_dump())
    return "Diagnosis received." + (f" Removed citations not seen in tool results: {dropped}" if dropped else ""), d


def execute(name: str, raw_args: str, ctx: ToolContext) -> tuple[str, DiagnosisArgs | None]:
    """Run one tool call. Returns (text for the model, diagnosis if this was the final call).
    Never raises: every failure becomes an error message the model can react to."""
    start = time.perf_counter()
    diagnosis = None
    error = None
    out = ""
    try:
        if name == "submit_diagnosis" and (raw_args or "").strip() in ("", "{}"):
            raise ValueError("submit_diagnosis was called with EMPTY arguments. Call it again and "
                             "fill in every required field.")
        parsed = json.loads(raw_args or "{}")
        if name == "search_past_tickets":
            out = _run_search(SearchArgs(**parsed), ctx)
        elif name == "get_ticket":
            out = _run_get(GetTicketArgs(**parsed), ctx)
        elif name == "log_template_stats":
            out = _run_template_stats(LogArgs(**parsed), ctx)
        elif name == "log_lines":
            out = _need_logs(ctx).raw_lines(**LogLinesArgs(**parsed).model_dump())
        elif name == "loki_labels":
            out = _need_logs(ctx).labels()
        elif name == "submit_diagnosis":
            out, diagnosis = _submit(parsed, ctx)
        else:
            raise ValueError(f"unknown tool {name!r}")
    except json.JSONDecodeError as e:
        error = f"arguments are not valid JSON: {e}"
    except ValidationError as e:
        error = "invalid arguments: " + "; ".join(
            f"{'.'.join(map(str, x['loc'])) or 'diagnosis'}: {x['msg']}" for x in e.errors())
    except httpx.HTTPError as e:
        error = f"log store unavailable: {e}"
    except (LookupError, ValueError) as e:  # includes LogQueryError
        error = str(e)

    if error:
        diagnosis = None
    text = f"ERROR: {error}" if error else out
    ctx.trace.append(TraceEvent(name, (raw_args or "")[:300], len(text),
                                int((time.perf_counter() - start) * 1000), error))
    return text, diagnosis