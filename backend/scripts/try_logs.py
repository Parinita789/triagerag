"""Run log_template_stats on the first anomalous and first normal block in the labels."""
import csv
from datetime import datetime, timedelta
from pathlib import Path

from triagerag.query.logs import LogQueryError, LogTools


def find(name):
    return next(Path("data").rglob(name))


labels = list(csv.DictReader(find("anomaly_label.csv").open()))
picks = {"anomalous": next(r["BlockId"] for r in labels if r["Label"] == "Anomaly"),
         "normal": next(r["BlockId"] for r in labels if r["Label"] == "Normal")}

first: dict[str, datetime] = {}
with find("HDFS.log").open(errors="replace") as f:
    for line in f:
        for kind, blk in picks.items():
            if kind not in first and f"{blk} " in line + " ":
                first[kind] = datetime.strptime(line[:13], "%y%m%d %H%M%S")
        if len(first) == 2:
            break

tools = LogTools()
print(tools.labels(), "\n")
for kind, blk in picks.items():
    t = first[kind]
    print(f"=== {kind} {blk} (first seen {t})")
    print(tools.template_stats(start=(t - timedelta(minutes=1)).isoformat(),
                               end=(t + timedelta(hours=1)).isoformat(), block_id=blk), "\n")

try:
    tools.template_stats(start="2008-11-10T00:00:00", end="2008-11-10T10:00:00")
except LogQueryError as e:
    print("guardrail:", e)