"""Judge vs hand labels: agreement, Cohen's kappa, and correctness when the report didn't already state the cause."""
import csv
import json
from collections import Counter
from pathlib import Path

EVAL = Path("data/eval")
judge = {r["key"]: r["verdict"] for r in json.loads((EVAL / "diagnosis_eval.json").read_text())}
mapping = json.loads((EVAL / "hand_label_map.json").read_text())
import argparse
ap = argparse.ArgumentParser()
ap.add_argument("--labels", default="hand_labels.csv")
labels_file = ap.parse_args().labels
with (EVAL / labels_file).open() as f:
    rows = [r for r in csv.DictReader(f) if r["label"].strip()]
if not rows:
    raise SystemExit("no labels filled in yet")

pairs = [(r["label"].strip(), judge[mapping[r["id"]]]) for r in rows]
n = len(pairs)
po = sum(h == j for h, j in pairs) / n
labels = sorted({x for p in pairs for x in p})
ph, pj = Counter(h for h, _ in pairs), Counter(j for _, j in pairs)
pe = sum((ph[l] / n) * (pj[l] / n) for l in labels)  # agreement expected by chance
kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
print(f"labels: {n}   exact agreement: {po:.0%}   Cohen's kappa: {kappa:.2f}")
print(f"agreement on correct vs not: {sum((h == 'correct') == (j == 'correct') for h, j in pairs)}/{n}")
print(f"\n{'you \\ judge':<14}" + "".join(f"{l:>14}" for l in labels))
for h in labels:
    print(f"{h:<14}" + "".join(f"{sum(1 for a, b in pairs if a == h and b == j):>14}" for j in labels))


def split(rs: list[dict], name: str) -> None:
    graded = [r for r in rs if r["label"].strip() in ("correct", "partial", "wrong")]
    you = sum(r["label"].strip() == "correct" for r in graded)
    jd = sum(judge[mapping[r["id"]]] == "correct" for r in graded)
    print(f"  {name:<28} graded {len(graded):>2}   correct by you {you:>2}   by judge {jd:>2}")


print("\ncause already stated in the report as filed?")
split([r for r in rows if r["cause_in_report"].strip().lower() == "yes"], "yes (diagnosis can restate)")
split([r for r in rows if r["cause_in_report"].strip().lower() == "no"], "no (the real test)")