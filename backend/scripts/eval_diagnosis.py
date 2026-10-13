"""Diagnosis-quality eval: an LLM judge compares each diagnosis's likely cause with what the
engineers concluded in the ticket's own comments (which the agent never saw).
Also writes a blind sheet for hand-labelling, to measure how far the judge can be trusted."""
import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path

from openai import OpenAIError
import psycopg
from pydantic import BaseModel, ValidationError

from triagerag.config import settings
from triagerag.eval.queries import load_dev_queries
from triagerag.index.chunk import render
from triagerag.query.agent import run_agent
from triagerag.query.llm import LLMClient
from triagerag.query.search import SearchService
from triagerag.query.tools import ToolContext
from triagerag.shared.clean import clean_ticket

SEED = 11
OUT = Path("data/eval/diagnosis_eval.json")
SHEET = Path("data/eval/hand_label.md")
LABELS_CSV = Path("data/eval/hand_labels.csv")
N_HAND = 20
MAX_TICKET_CHARS = 3000
MAX_REF_CHARS = 8000

JUDGE_SYSTEM = """You grade automated diagnoses of HDFS bug reports against what the engineers who fixed the bug concluded.
You get: the bug report as filed, the engineers' comments on it (written later, while fixing it), and a diagnosis written from the report alone.
1. From the comments, state in one sentence the root cause the engineers identified. If the comments never say what caused the bug (for example only reviews and "+1, committed"), write "not stated".
2. Compare the diagnosis's likely cause with it:
- correct: the same root cause: same component and same mechanism. Wording may differ; extra detail is fine if it doesn't contradict.
- partial: the right component or area, but a vague, incomplete or different mechanism; or the right cause hedged among several unrelated ones.
- wrong: a different cause, or one that contradicts the comments.
- no_reference: the comments don't state a cause.
Judge only the cause. Ignore writing quality, confidence and cited tickets. Do not reward length.
Call submit_verdict exactly once."""

VERDICT_TOOL = [{"type": "function", "function": {
    "name": "submit_verdict",
    "description": "Submit the grade.",
    "parameters": {"type": "object", "properties": {
        "reference_cause": {"type": "string",
                            "description": "One sentence: the root cause in the comments, or 'not stated'"},
        "reasoning": {"type": "string",
                      "description": "Compare the diagnosis's likely cause with the reference cause"},
        "verdict": {"type": "string", "enum": ["correct", "partial", "wrong", "no_reference"]},
    }, "required": ["reference_cause", "reasoning", "verdict"]},
}}]


class Verdict(BaseModel):
    reference_cause: str
    reasoning: str
    verdict: str


def reference_text(raw: dict) -> str:
    t = clean_ticket(raw)
    parts = [render(c.text) for c in t.comments]
    return "\n\n---\n\n".join(p for p in parts if p.strip())[:MAX_REF_CHARS]


def judge(llm: LLMClient, ticket: str, reference: str, summary: str, cause: str) -> Verdict | None:
    messages = [
        {"role": "system", "content": JUDGE_SYSTEM},
        {"role": "user", "content": (
            f"BUG REPORT AS FILED:\n{ticket[:MAX_TICKET_CHARS]}\n\n"
            f"ENGINEERS' COMMENTS:\n{reference or '(no comments)'}\n\n"
            f"DIAGNOSIS SUMMARY:\n{summary}\n\nDIAGNOSIS LIKELY CAUSE:\n{cause}")},
    ]
    try:
        resp = llm.chat(messages, VERDICT_TOOL)
    except OpenAIError:
        return None  # recorded as judge_error; re-run later to retry
    calls = resp["choices"][0]["message"].get("tool_calls") or []
    if not calls:
        return None
    try:
        return Verdict(**json.loads(calls[0]["function"]["arguments"]))
    except (json.JSONDecodeError, ValidationError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=40)
    args = ap.parse_args()
    if not settings.judge_model:
        raise SystemExit("set JUDGE_MODEL in .env (a stronger model from a different family than the agent)")

    agent_llm = LLMClient(settings.openrouter_api_key, settings.openrouter_model)
    judge_llm = LLMClient(settings.openrouter_api_key, settings.judge_model)

    rows = []
    with psycopg.connect(settings.database_url) as conn:
        sample = random.Random(SEED).sample(load_dev_queries(conn), args.n)
        service = SearchService(conn)
        for i, q in enumerate(sample, 1):
            ctx = ToolContext(service=service, before=q.created_at, exclude=q.key)
            r = run_agent(q.text, ctx, agent_llm, extra_calls=0)
            row = {"key": q.key, "stopped": r.stopped,
                   "retrieval_hit": any(k in q.relevant for k in r.related)}
            if r.stopped == "llm_error":
                print(f"  {i}/{len(sample)} {q.key}: llm_error, stopping (re-run continues from the cache)")
                break
            d = r.diagnosis
            if d is None or d.abstain:
                row["verdict"] = "abstained" if d else "no_diagnosis"
                rows.append(row)
                print(f"  {i}/{len(sample)} {q.key}: {row['verdict']}")
                continue

            raw = conn.execute("select raw from tickets where key = %s", (q.key,)).fetchone()[0]
            ref = reference_text(raw)
            v = judge(judge_llm, q.text, ref, d.summary, d.likely_cause)
            row.update({
                "confidence": d.confidence, "summary": d.summary, "likely_cause": d.likely_cause,
                "ticket": q.text[:MAX_TICKET_CHARS], "reference": ref,
                "reference_cause": v.reference_cause if v else None,
                "judge_reasoning": v.reasoning if v else None,
                "verdict": v.verdict if v else "judge_error",
            })
            rows.append(row)
            print(f"  {i}/{len(sample)} {q.key}: {row['verdict']}")

    OUT.write_text(json.dumps(rows, indent=2))
    write_blind_sheet(rows)
    report(rows)


def write_blind_sheet(rows: list[dict]) -> None:
    """Hand-label these WITHOUT looking at diagnosis_eval.json, then fill in hand_labels.csv."""
    judged = [r for r in rows if r["verdict"] in ("correct", "partial", "wrong", "no_reference")][:N_HAND]
    out = ["# Hand labels (blind)\n",
           "For each ticket: read the comments, decide the root cause, then label the diagnosis's likely cause:",
           "correct / partial / wrong / no_reference (same rules as the judge). Write labels in hand_labels.csv.\n"]
    for r in judged:
        out += [f"\n## {r['key']}\n", "### Bug report as filed\n", r["ticket"], "\n### Engineers' comments\n",
                r["reference"] or "(no comments)", "\n### Diagnosis likely cause\n", r["likely_cause"], "\n"]
    SHEET.write_text("\n".join(out))
    if not LABELS_CSV.exists():  # never overwrite labels you've already written
        with LABELS_CSV.open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["key", "label"])
            for r in judged:
                w.writerow([r["key"], ""])
    print(f"\nblind sheet: {SHEET}   labels to fill: {LABELS_CSV}")


def report(rows: list[dict]) -> None:
    counts = Counter(r["verdict"] for r in rows)
    graded = [r for r in rows if r["verdict"] in ("correct", "partial", "wrong")]
    print(f"\n[diagnosis] agent={settings.openrouter_model}  judge={settings.judge_model}  tickets={len(rows)}")
    print("  " + "  ".join(f"{k}: {v}" for k, v in counts.most_common()))
    if not graded:
        return
    n = len(graded)
    print(f"  of {n} graded: correct {counts['correct']} ({counts['correct'] / n:.0%})  "
          f"partial {counts['partial']}  wrong {counts['wrong']}")

    def rate(rs: list[dict]) -> str:
        return f"{sum(r['verdict'] == 'correct' for r in rs)}/{len(rs)} correct" if rs else "none"

    print(f"  linked ticket retrieved: {rate([r for r in graded if r['retrieval_hit']])}   "
          f"not retrieved: {rate([r for r in graded if not r['retrieval_hit']])}")
    by_conf = defaultdict(list)
    for r in graded:
        by_conf[r["confidence"]].append(r)
    print("  by confidence: " + "   ".join(f"{c}: {rate(by_conf[c])}" for c in ("high", "medium", "low")))


if __name__ == "__main__":
    main()