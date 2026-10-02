import json
from dataclasses import dataclass, field

from openai import OpenAIError

from triagerag.query.llm import LLMClient
from triagerag.query.tools import (LOG_TOOLS, TICKET_TOOLS, DiagnosisArgs, ToolContext, TraceEvent,
                                   execute)

INITIAL_K = 5
MAX_TOKENS = 60_000
TICKET_SUBMIT_ONLY = [t for t in TICKET_TOOLS if t["function"]["name"] == "submit_diagnosis"]


def _ticket_prompt(extra_calls: int) -> str:
    prompt = """You are an experienced HDFS engineer triaging a newly filed bug report.
You are given the report and the results of a search for related past tickets.
- Cite only tickets that appear in results you were given. Never invent ticket keys.
- List every ticket you rely on in related_tickets. Naming a ticket only in your summary doesn't count.
- If the evidence doesn't explain the failure, submit with abstain=true and say why.
- Finish by calling submit_diagnosis exactly once."""
    if extra_calls:
        prompt += f"""
- If the results look weak or unrelated, you may search again with different words (the symptom,
  the component, exact class or error names) or read a ticket with get_ticket.
  You have at most {extra_calls} such calls; then you must submit."""
    return prompt


def _log_prompt(max_queries: int) -> str:
    return f"""You are an experienced HDFS engineer investigating a report about one block,
using the cluster's logs. Log times are UTC.
- Start with log_template_stats for the block_id, over a range around the reported time
  (for example 1 hour before to 1 hour after).
- If the result says the block's write-path lines are not in range, query again around when the
  block was written before concluding anything is missing.
- Weigh the evidence. Exceptions, errors and MISSING write-path lines are strong evidence.
  RARE only means uncommon: some rare lines record routine recovery work and also appear on
  healthy blocks, so a rare line alone is weak evidence.
- A block whose lifecycle looks normal gets anomaly_found=false. That is a valid answer.
- Use confidence=high only for clear exceptions/errors or missing lines.
- You have at most {max_queries} log tool calls; then call submit_diagnosis exactly once."""


@dataclass
class AgentResult:
    diagnosis: DiagnosisArgs | None
    stopped: str            # submitted | no_submit | budget | llm_error
    related: list[str]      # top tickets from the automatic search: postable even with no diagnosis
    llm_calls: int
    extra_calls: int        # tool calls the model chose to make
    tokens: int
    trace: list[TraceEvent] = field(default_factory=list)
    error: str | None = None


def _loop(messages: list[dict], tools: list[dict], ctx: ToolContext, llm: LLMClient,
          extra_calls: int, related: list[str]) -> AgentResult:
    allowed = {t["function"]["name"] for t in tools}
    llm_calls = used = tokens = 0
    nudged = False

    def done(diagnosis: DiagnosisArgs | None, stopped: str, error: str | None = None) -> AgentResult:
        return AgentResult(diagnosis, stopped, related, llm_calls, used, tokens, ctx.trace, error)

    while True:
        try:
            resp = llm.chat(messages, tools)
        except OpenAIError as e:
            return done(None, "llm_error", str(e)[:300])
        llm_calls += 1
        tokens += (resp.get("usage") or {}).get("total_tokens", 0)

        msg = resp["choices"][0]["message"]
        tool_calls = msg.get("tool_calls") or []
        assistant = {"role": "assistant", "content": msg.get("content") or ""}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)

        if tokens > MAX_TOKENS:
            return done(None, "budget")

        if not tool_calls:
            if nudged:
                return done(None, "no_submit")
            messages.append({"role": "user",
                             "content": "Call submit_diagnosis now with your conclusion, or abstain."})
            nudged = True
            continue

        for tc in tool_calls:
            name = tc["function"]["name"]
            args = tc["function"].get("arguments", "")
            if name not in allowed:
                text, diagnosis = f"ERROR: tool {name!r} is not available. Call submit_diagnosis.", None
            elif name != "submit_diagnosis" and used >= extra_calls:
                text, diagnosis = "ERROR: no calls left. Call submit_diagnosis now.", None
            else:
                if name != "submit_diagnosis":
                    used += 1
                text, diagnosis = execute(name, args, ctx)
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text})
            if diagnosis is not None:
                return done(diagnosis, "submitted")

        if llm_calls > extra_calls + 3:
            return done(None, "budget")


def run_agent(ticket_text: str, ctx: ToolContext, llm: LLMClient, extra_calls: int = 3) -> AgentResult:
    """Jira ticket: retrieval first (no LLM), then the LLM diagnoses."""
    results = execute("search_past_tickets",
                      json.dumps({"query": ticket_text[:500], "k": INITIAL_K}), ctx)[0]
    related = list(ctx.last_search)

    tools = TICKET_TOOLS if extra_calls else TICKET_SUBMIT_ONLY
    instruction = ("If these explain the failure, submit your diagnosis. If they look weak or unrelated, "
                   "search again with different words or read a ticket first."
                   if extra_calls else "Write your diagnosis from these results.")
    messages: list[dict] = [
        {"role": "system", "content": _ticket_prompt(extra_calls)},
        {"role": "user", "content": f"New ticket:\n\n{ticket_text}\n\n"
                                    f"Related past tickets (search on the ticket text):\n\n{results}\n\n"
                                    f"{instruction}"},
    ]
    return _loop(messages, tools, ctx, llm, extra_calls, related)


def run_log_agent(ticket_text: str, ctx: ToolContext, llm: LLMClient, max_queries: int = 4) -> AgentResult:
    """Ticket whose evidence is in the logs: no ticket retrieval; the LLM investigates with log tools."""
    messages: list[dict] = [
        {"role": "system", "content": _log_prompt(max_queries)},
        {"role": "user", "content": f"New ticket:\n\n{ticket_text}\n\n"
                                    "Investigate with the log tools, then call submit_diagnosis."},
    ]
    return _loop(messages, LOG_TOOLS, ctx, llm, max_queries, related=[])