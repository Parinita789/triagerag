"""Train Drain on the HDFS cluster logs; find templates that separate anomalous blocks from normal ones.

Signatures are derived from the labels, so they are used ONLY to score the agent, never by its tools.
"""
import csv
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from drain3.file_persistence import FilePersistence

from triagerag.index.logs.drain import build_miner

OUT_MODEL = Path("models/drain_hdfs.bin")
OUT_SIG = Path("data/eval/hdfs_signatures.json")
BLK = re.compile(r"blk_-?\d+")
MIN_ANOMALOUS = 20  # a signature appears in at least 20 anomalous blocks...
MIN_RATIO = 0.5     # ...and at least half the blocks containing it are anomalous
OUT_BASE = Path("models/hdfs_template_baseline.json")


def find(name: str) -> Path:
    hits = list(Path("data").rglob(name))
    if not hits:
        sys.exit(f"{name} not found under data/")
    return hits[0]


def main() -> None:
    log, labels = find("HDFS.log"), find("anomaly_label.csv")
    with labels.open() as f:
        anomalous = {r["BlockId"] for r in csv.DictReader(f) if r["Label"] == "Anomaly"}

    miner = build_miner(None)  # train in memory; saved once at the end
    block_templates: dict[str, set[int]] = defaultdict(set)
    start = time.time()

    with log.open(errors="replace") as f:
        for n, line in enumerate(f, 1):
            parts = line.rstrip("\n").split(" ", 5)
            if len(parts) < 6:
                continue
            msg = parts[5]  # text after "component:"
            cid = miner.add_log_message(msg)["cluster_id"]
            for blk in set(BLK.findall(msg)):
                block_templates[blk].add(cid)
            if n % 1_000_000 == 0:
                print(f"  {n:,} lines  {n / (time.time() - start):,.0f}/s", flush=True)

    templates = {c.cluster_id: c.get_template() for c in miner.drain.clusters}
    blocks_with: Counter = Counter()
    anom_with: Counter = Counter()
    for blk, tids in block_templates.items():
        is_bad = blk in anomalous
        for t in tids:
            blocks_with[t] += 1
            anom_with[t] += is_bad

    n_blocks = len(block_templates)
    n_bad = len(anomalous & block_templates.keys())
    OUT_BASE.write_text(json.dumps({
        str(t): {"template": templates[t], "block_share": blocks_with[t] / n_blocks}
        for t in templates
    }, indent=2))
    print(f"\ntemplates: {len(templates)}   blocks: {n_blocks:,}   anomalous: {n_bad:,} ({n_bad / n_blocks:.1%})\n")
    print(f"{'id':>4} {'blocks':>9} {'anomalous':>9} {'ratio':>6}  template")
    for t in sorted(templates, key=lambda t: -(anom_with[t] / blocks_with[t]) if blocks_with[t] else 0):
        if blocks_with[t]:
            print(f"{t:>4} {blocks_with[t]:>9,} {anom_with[t]:>9,} {anom_with[t] / blocks_with[t]:>6.0%}  {templates[t][:90]}")

    sig = {t for t in templates
           if anom_with[t] >= MIN_ANOMALOUS and anom_with[t] / blocks_with[t] >= MIN_RATIO}
    covered = sum(1 for b in anomalous if block_templates.get(b, set()) & sig)
    false_hits = sum(1 for b, tids in block_templates.items() if b not in anomalous and tids & sig)
    print(f"\nsignature templates: {len(sig)}")
    print(f"anomalous blocks with a signature: {covered:,}/{n_bad:,} ({covered / n_bad:.1%})")
    print(f"normal blocks with a signature:    {false_hits:,}/{n_blocks - n_bad:,}")

    OUT_SIG.parent.mkdir(parents=True, exist_ok=True)
    OUT_SIG.write_text(json.dumps({
        "min_anomalous": MIN_ANOMALOUS, "min_ratio": MIN_RATIO,
        "templates": {str(t): templates[t] for t in sorted(sig)},
    }, indent=2))
    miner.persistence_handler = FilePersistence(str(OUT_MODEL))
    miner.save_state("trained on HDFS v1")
    print(f"saved {OUT_MODEL} and {OUT_SIG}")


if __name__ == "__main__":
    main()