import argparse

import psycopg

from triagerag.config import settings
from triagerag.eval.queries import load_dev_queries
from triagerag.query.agent import run_agent
from triagerag.query.llm import LLMClient
from triagerag.query.search import SearchService
from triagerag.query.tools import ToolContext


def mark(key: str, relevant: dict) -> str:
    return "✅" if key in relevant else "  "


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("key", nargs="?", help="dev ticket key; default: first dev query")
    ap.add_argument("--extra-calls", type=int, default=3, help="0 = pipeline mode, one LLM call")
    args = ap.parse_args()

    with psycopg.connect(settings.database_url) as conn:
        queries = load_dev_queries(conn)
        q = next(q for q in queries if q.key == args.key) if args.key else queries[0]
        service = SearchService(conn)
        llm = LLMClient(settings.openrouter_api_key, settings.openrouter_model)
        ctx = ToolContext(service=service, before=q.created_at, exclude=q.key)
        r = run_agent(q.text, ctx, llm, extra_calls=args.extra_calls)

    print(f"TICKET {q.key}   answer: {list(q.relevant)}   model: {settings.openrouter_model}")
    print(f"stopped: {r.stopped}   llm calls: {r.llm_calls}   extra calls: {r.extra_calls}   "
          f"tokens: {r.tokens:,}")
    if r.error:
        print(f"error: {r.error}")

    print("\nretrieval (no LLM):")
    for k in r.related:
        print(f"  {mark(k, q.relevant)} {k}")

    print("\ntrace:")
    for e in r.trace:
        flag = f"  ERROR: {e.error}" if e.error else ""
        print(f"  {e.tool}({e.args[:80]}) → {e.result_chars} chars, {e.ms} ms{flag}")

    d = r.diagnosis
    if d:
        print(f"\nconfidence: {d.confidence}   abstain: {d.abstain}")
        print(f"summary: {d.summary}\nlikely cause: {d.likely_cause}")
        for t in d.related_tickets:
            print(f"  {mark(t.key, q.relevant)} {t.key}: {t.why}")
        if d.abstain:
            print(f"abstain reason: {d.abstain_reason}")

    retrieval_hit = any(k in q.relevant for k in r.related)
    agent_hit = bool(d) and any(t.key in q.relevant for t in d.related_tickets)
    print(f"\nlinked ticket in retrieval top {len(r.related)}: {retrieval_hit}   "
          f"cited by the LLM: {agent_hit}")


if __name__ == "__main__":
    main()