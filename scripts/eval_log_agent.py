"""Evaluate the log agent on data/eval/log_tickets.json. Expected/missing templates are used for scoring only."""
import argparse
import json
from pathlib import Path

from triagerag.config import settings
from triagerag.query.agent import run_log_agent
from triagerag.query.llm import LLMClient
from triagerag.query.logs import LogTools
from triagerag.query.tools import DiagnosisArgs, ToolContext

EVAL = Path("data/eval/log_tickets.json")
OUT = Path("data/eval/log_agent.json")


def score(item: dict, d: DiagnosisArgs | None, weak: set[int]) -> str:
    if d is None or d.abstain or d.anomaly_found is None:
        return "no_verdict"
    cited = {e.template_id for e in d.evidence_templates}
    if item["label"] == "anomalous":
        if not d.anomaly_found:
            return "missed"
        good = set(item["expected_templates"]) | set(item["missing_templates"])
        return "caught" if cited & good else "wrong_evidence"
    if not d.anomaly_found:
        return "cleared"
    if cited <= weak and d.confidence == "low":
        return "cleared_low"   # flagged only weak lines, at low confidence: acceptable
    return "false_alarm"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="run one ticket, e.g. L02, and print its trace")
    ap.add_argument("--n", type=int, default=0, help="only the first N tickets")
    ap.add_argument("--max-queries", type=int, default=4)
    args = ap.parse_args()

    data = json.loads(EVAL.read_text())
    weak = set(data["weak"])
    items = data["items"]
    if args.id:
        items = [it for it in items if it["id"] == args.id]
    elif args.n:
        items = items[: args.n]

    logs = LogTools()
    llm = LLMClient(settings.openrouter_api_key, settings.openrouter_model)
    rows = []
    for i, it in enumerate(items, 1):
        ctx = ToolContext(service=None, before=None, logs=logs)
        r = run_log_agent(it["ticket"], ctx, llm, max_queries=args.max_queries)
        d = r.diagnosis
        verdict = score(it, d, weak) if r.stopped != "llm_error" else "llm_error"
        print(f"  {i}/{len(items)} {it['id']} {it['label']:<9} {it['kind']:<12} {r.stopped:<10} {verdict}")

        if args.id:
            for ev in r.trace:
                print(f"    {ev.tool}({ev.args[:140]}) -> {ev.result_chars} chars"
                      + (f"  ERROR: {ev.error}" if ev.error else ""))
            if d:
                print(d.model_dump_json(indent=2))
            print(f"    truth: {it['label']} {it['kind']}  expected {it['expected_templates']}  "
                  f"missing {it['missing_templates']}  rare {it['rare_templates']}")

        if r.stopped == "llm_error":
            print(f"    {r.error}\n    stopping; re-run later to continue from the cache")
            break
        rows.append({
            "id": it["id"], "label": it["label"], "kind": it["kind"], "stopped": r.stopped,
            "verdict": verdict, "anomaly_found": d.anomaly_found if d else None,
            "confidence": d.confidence if d else None,
            "cited": [e.template_id for e in d.evidence_templates] if d else [],
            "llm_calls": r.llm_calls, "log_calls": r.extra_calls, "tokens": r.tokens,
            "tool_errors": sum(1 for e in r.trace if e.error),
            "first_error": next((e.error for e in r.trace if e.error), None),
        })

    if not rows or args.id:
        return
    OUT.write_text(json.dumps(rows, indent=2))
    anom = [r for r in rows if r["label"] == "anomalous"]
    norm = [r for r in rows if r["label"] == "normal"]
    n = len(rows)

    def count(rs, *v):
        return sum(r["verdict"] in v for r in rs)

    print(f"\n[log agent] model={settings.openrouter_model}  tickets={n}")
    if anom:
        print(f"  anomalous caught with right evidence: {count(anom, 'caught')}/{len(anom)}"
              f"   (missed {count(anom, 'missed')}, wrong evidence {count(anom, 'wrong_evidence')})")
    if norm:
        print(f"  normal cleared:                       {count(norm, 'cleared', 'cleared_low')}/{len(norm)}"
              f"   (false alarms {count(norm, 'false_alarm')})")
    print(f"  no verdict: {count(rows, 'no_verdict')}   tool errors: {sum(r['tool_errors'] for r in rows)}")
    print(f"  avg llm calls: {sum(r['llm_calls'] for r in rows) / n:.1f}   "
          f"avg log calls: {sum(r['log_calls'] for r in rows) / n:.1f}   "
          f"avg tokens: {sum(r['tokens'] for r in rows) / n:,.0f}")


if __name__ == "__main__":
    main()