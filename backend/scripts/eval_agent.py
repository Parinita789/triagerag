import argparse
import json
import random
from pathlib import Path

import psycopg

from triagerag.config import settings
from triagerag.eval.queries import load_dev_queries
from triagerag.query.agent import run_agent
from triagerag.query.llm import LLMClient
from triagerag.query.search import SearchService
from triagerag.query.tools import ToolContext

OUT = Path("data/eval")
SAMPLE_SEED = 7


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--extra-calls", type=int, default=3)
    args = ap.parse_args()
    mode = "pipeline" if args.extra_calls == 0 else f"hybrid{args.extra_calls}"

    with psycopg.connect(settings.database_url) as conn:
        dev = load_dev_queries(conn)
        sample = random.Random(SAMPLE_SEED).sample(dev, args.n)
        service = SearchService(conn)
        llm = LLMClient(settings.openrouter_api_key, settings.openrouter_model)

        rows = []
        for i, q in enumerate(sample, 1):
            ctx = ToolContext(service=service, before=q.created_at, exclude=q.key)
            r = run_agent(q.text, ctx, llm, extra_calls=args.extra_calls)
            cited = [t.key for t in r.diagnosis.related_tickets] if r.diagnosis else []
            rows.append({
                "key": q.key,
                "stopped": r.stopped,
                "retrieval_hit": any(k in q.relevant for k in r.related),
                "agent_hit": any(k in q.relevant for k in cited),
                "abstain": bool(r.diagnosis and r.diagnosis.abstain),
                "confidence": r.diagnosis.confidence if r.diagnosis else None,
                "cited": cited,
                "llm_calls": r.llm_calls,
                "extra_calls": r.extra_calls,
                "tokens": r.tokens,
                "tool_errors": sum(1 for e in r.trace if e.error),
            })
            print(f"  {i}/{len(sample)} {q.key}: {r.stopped}")
            if r.stopped == "llm_error":
                print(f"    {r.error}\n    stopping; re-run later to continue from the cache")
                break

    done = [r for r in rows if r["stopped"] != "llm_error"]
    n = len(done)
    if not n:
        return
    print(f"\n[{mode}] model={settings.openrouter_model}  tickets={n}")
    print(f"  linked ticket in retrieval top 5:  {sum(r['retrieval_hit'] for r in done)}/{n}")
    print(f"  linked ticket cited by the LLM:    {sum(r['agent_hit'] for r in done)}/{n}")
    print(f"  found by LLM, missed by retrieval: "
          f"{sum(r['agent_hit'] and not r['retrieval_hit'] for r in done)}")
    print(f"  in retrieval but not cited:        "
          f"{sum(r['retrieval_hit'] and not r['agent_hit'] for r in done)}")
    print(f"  abstained: {sum(r['abstain'] for r in done)}   "
          f"no_submit: {sum(r['stopped'] == 'no_submit' for r in done)}   "
          f"tool errors: {sum(r['tool_errors'] for r in done)}")
    print(f"  avg llm calls: {sum(r['llm_calls'] for r in done) / n:.1f}   "
          f"avg tokens: {sum(r['tokens'] for r in done) / n:,.0f}")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"dev_agent_{mode}.json").write_text(json.dumps(rows, indent=1))


if __name__ == "__main__":
    main()