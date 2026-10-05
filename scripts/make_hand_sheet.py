"""Blind hand-label sheet: neutral IDs, shuffled order, ticket key masked, no judge output."""
import csv
import json
import random
import re
from pathlib import Path

EVAL = Path("data/eval")
rows = [r for r in json.loads((EVAL / "diagnosis_eval.json").read_text())
        if r["verdict"] in ("correct", "partial", "wrong", "no_reference")]
random.Random(99).shuffle(rows)
ids = {f"H{i:02d}": r["key"] for i, r in enumerate(rows, 1)}
(EVAL / "hand_label_map.json").write_text(json.dumps(ids, indent=2))

out = ["# Hand labels (blind)\n",
       "For each item: read the report and the comments, then fill in hand_labels.csv:",
       "- label: correct / partial / wrong / no_reference (same rules as the judge)",
       "- cause_in_report: yes if the bug report AS FILED already states the root cause, else no\n"]
for hid, r in zip(ids, rows):
    ref = re.sub(rf"\b{re.escape(r['key'])}\b", "THIS-TICKET", r["reference"] or "(no comments)")
    out += [f"\n---\n## {hid}\n", "### Bug report as filed\n", r["ticket"],
            "\n### Engineers' comments\n", ref, "\n### Diagnosis likely cause\n", r["likely_cause"], "\n"]
(EVAL / "hand_label.md").write_text("\n".join(out))

csv_path = EVAL / "hand_labels.csv"
if not csv_path.exists():  # never overwrite labels you've written
    with csv_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "label", "cause_in_report"])
        w.writerows([[hid, "", ""] for hid in ids])
print(f"{len(ids)} items -> {EVAL / 'hand_label.md'}, fill in {csv_path}")