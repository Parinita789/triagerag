import json
from dataclasses import dataclass, field

from openai import OpenAIError

from triagerag.query.llm import LLMClient
from triagerag.query.tools import TOOLS, DiagnosisArgs, ToolContext, TraceEvent, execute

INITIAL_K = 5
MAX_TOKENS = 60_000
SUBMIT_ONLY = [t for t in TOOLS if t["function"]["name"] == "submit_diagnosis"]

MAX_TICKET_CHARS = 8_000  # ~2k tokens

def _clip(text: str) -> str:
    if len(text) <= MAX_TICKET_CHARS:
        return text
    head = text[: MAX_TICKET_CHARS - 1_000]
    tail = text[-1_000:]
    return f"{head}\n...[truncated {len(text) - MAX_TICKET_CHARS} chars]...\n{tail}"


def _system_prompt(extra_calls: int) -> str:
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


@dataclass
class AgentResult:
    diagnosis: DiagnosisArgs | None
    stopped: str            # submitted | no_submit | budget | llm_error
    related: list[str]      # top tickets from the automatic search: postable even with no diagnosis
    llm_calls: int
    extra_calls: int        # searches and reads the model chose to make
    tokens: int
    trace: list[TraceEvent] = field(default_factory=list)
    error: str | None = None


def run_agent(ticket_text: str, ctx: ToolContext, llm: LLMClient, extra_calls: int = 3) -> AgentResult:
    # 1. Retrieval: RRF on the ticket text. No LLM.
    results = execute("search_past_tickets",
                      json.dumps({"query": ticket_text[:500], "k": INITIAL_K}), ctx)[0]
    related = list(ctx.last_search)

    # 2. The LLM reads the ticket + results.
    tools = TOOLS if extra_calls else SUBMIT_ONLY
    allowed = {t["function"]["name"] for t in tools}
    instruction = ("If these explain the failure, submit your diagnosis. If they look weak or unrelated, "
                   "search again with different words or read a ticket first."
                   if extra_calls else "Write your diagnosis from these results.")
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(extra_calls)},
        {"role": "user", "content": f"New ticket:\n\n{ticket_text}\n\n"
                                    f"Related past tickets (search on the ticket text):\n\n{results}\n\n"
                                    f"{instruction}"},
    ]

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