import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from triagerag.query.search import SearchService

TICKET_KEY = r"^HDFS-\d+$"


class SearchArgs(BaseModel):
    query: str = Field(min_length=3, max_length=500)
    k: int = Field(default=5, ge=1, le=8)


class GetTicketArgs(BaseModel):
    key: str = Field(pattern=TICKET_KEY)


class RelatedTicket(BaseModel):
    key: str = Field(pattern=TICKET_KEY)
    why: str


class DiagnosisArgs(BaseModel):
    summary: str
    likely_cause: str
    related_tickets: list[RelatedTicket] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]
    abstain: bool
    abstain_reason: str = ""

    @model_validator(mode="after")
    def _cite_when_answering(self) -> "DiagnosisArgs":
        if not self.abstain and not self.related_tickets:
            raise ValueError("related_tickets is empty: list every ticket your diagnosis relies on, "
                             "or set abstain=true")
        return self


TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {
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
    }},
    {"type": "function", "function": {
        "name": "get_ticket",
        "description": (
            "Read one past ticket in more detail: its description and up to 6 comments. Use this before "
            "relying on a ticket from search results. Returns an error for tickets that don't exist or "
            "weren't resolved yet."),
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string", "description": "Ticket key, e.g. HDFS-1234"},
        }, "required": ["key"]},
    }},
    {"type": "function", "function": {
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
    }},
]


@dataclass
class TraceEvent:
    tool: str
    args: str
    result_chars: int
    ms: int
    error: str | None = None


@dataclass
class ToolContext:
    service: SearchService
    before: datetime          # the query ticket's creation time; never shown to the model
    exclude: str              # the query ticket's own key
    seen: set[str] = field(default_factory=set)
    last_search: list[str] = field(default_factory=list)
    trace: list[TraceEvent] = field(default_factory=list)


def _excerpt(snippet: str) -> str:
    body = snippet.split("\n\n", 1)[-1]          # drop the "[KEY] summary" header
    return re.sub(r"\s+", " ", body).strip()


def _run_search(args: SearchArgs, ctx: ToolContext) -> str:
    hits = ctx.service.search(args.query, ctx.before, ctx.exclude, k=args.k)
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
    d = ctx.service.get_ticket(args.key, ctx.before, ctx.exclude)
    if d is None:
        raise LookupError(f"{args.key} not found or not resolved yet")
    ctx.seen.add(d.key)
    parts = [f"{d.key} [{d.issue_type}, {d.resolution}] {d.summary}",
             f"DESCRIPTION: {_excerpt(d.description)}"]
    parts += [f"COMMENT {i}: {_excerpt(c)}" for i, c in enumerate(d.comments, 1)]
    return "\n\n".join(parts)


def execute(name: str, raw_args: str, ctx: ToolContext) -> tuple[str, DiagnosisArgs | None]:
    """Run one tool call. Returns (text for the model, diagnosis if this was the final call).
    Never raises: every failure becomes an error message the model can react to."""
    start = time.perf_counter()
    diagnosis = None
    error = None
    out = ""
    if name == "submit_diagnosis" and raw_args.strip() in ("", "{}"):
        return (
            "ERROR: submit_diagnosis was called with EMPTY arguments. "
            "Call it again and fill in every field: summary, likely_cause, "
            "confidence, abstain, related_tickets (key + reason for each).",
            None,
        )
    try:
        parsed = json.loads(raw_args or "{}")
        if name == "search_past_tickets":
            out = _run_search(SearchArgs(**parsed), ctx)
        elif name == "get_ticket":
            out = _run_get(GetTicketArgs(**parsed), ctx)
        elif name == "submit_diagnosis":
            diagnosis = DiagnosisArgs(**parsed)
            cited = [t for t in diagnosis.related_tickets if t.key in ctx.seen]
            dropped = [t.key for t in diagnosis.related_tickets if t.key not in ctx.seen]
            diagnosis.related_tickets = cited
            out = "Diagnosis received." + (f" Removed uncited tickets: {dropped}" if dropped else "")
        else:
            raise ValueError(f"unknown tool {name!r}")
    except json.JSONDecodeError as e:
        error = f"arguments are not valid JSON: {e}"
    except ValidationError as e:
        error = "invalid arguments: " + "; ".join(
            f"{'.'.join(map(str, x['loc'])) or 'diagnosis'}: {x['msg']}" for x in e.errors())
    except (LookupError, ValueError) as e:
        error = str(e)

    if error:
        diagnosis = None
    text = f"ERROR: {error}" if error else out
    ctx.trace.append(TraceEvent(name, raw_args[:300], len(text),
                                int((time.perf_counter() - start) * 1000), error))
    return text, diagnosis