"""How are anomalous HDFS blocks spread over time? Decides how the Loki agent eval is designed."""
import csv
import re
import sys
from collections import Counter
from pathlib import Path

BLK = re.compile(r"blk_-?\d+")


def find(name: str) -> Path:
    hits = list(Path("data").rglob(name))
    if not hits:
        sys.exit(f"{name} not found under data/")
    return hits[0]


def main() -> None:
    log, labels = find("HDFS.log"), find("anomaly_label.csv")

    anomalous = set()
    with labels.open() as f:
        for row in csv.DictReader(f):
            if row["Label"] == "Anomaly":
                anomalous.add(row["BlockId"])

    # each block is assigned to the hour it first appears in
    first_seen: dict[str, str] = {}
    with log.open(errors="replace") as f:
        for n, line in enumerate(f, 1):
            hour = line[:9]  # "081109 20" = 2008-11-09, 20:00
            for blk in BLK.findall(line):
                first_seen.setdefault(blk, hour)
            if n % 2_000_000 == 0:
                print(f"  {n:,} lines", flush=True)

    total = Counter(first_seen.values())
    bad = Counter(h for b, h in first_seen.items() if b in anomalous)

    print(f"\n{'hour':<10} {'blocks':>8} {'anomalous':>10} {'rate':>7}")
    for h in sorted(total):
        print(f"{h:<10} {total[h]:>8,} {bad[h]:>10,} {bad[h] / total[h]:>7.1%}")

    rates = [bad[h] / total[h] for h in total if total[h] >= 1000]
    print(f"\nhours: {len(total)}   anomalous blocks: {sum(bad.values()):,}")
    print(f"rate per hour (hours with 1k+ blocks): min {min(rates):.1%}  max {max(rates):.1%}")


if __name__ == "__main__":
    main()