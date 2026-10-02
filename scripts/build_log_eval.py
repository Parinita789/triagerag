"""Build the log-agent eval set: anomalous and normal HDFS blocks, each with the same kind of ticket.

The expected/missing/rare template fields are for SCORING ONLY; the agent never sees them.
"""
import csv
import json
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from triagerag.index.logs.drain import build_miner

SEED = 7
N_STRONG, N_MISSING, N_REPL, N_PLAIN = 10, 3, 5, 5
RARE = 0.02          # same thresholds as the tool
EXPECTED = 0.95
STRONG_RATIO = 0.95  # strong signature: 95%+ of blocks containing it are anomalous
WEAK_RATIO = 0.5
MIN_ANOMALOUS = 20

MODEL = Path("models/drain_hdfs.bin")
BASELINE = Path("models/hdfs_template_baseline.json")
OUT = Path("data/eval/log_tickets.json")
BLK = re.compile(r"blk_-?\d+")


def find(name: str) -> Path:
    hits = list(Path("data").rglob(name))
    if not hits:
        sys.exit(f"{name} not found under data/")
    return hits[0]


def ts(s: str) -> datetime:
    return datetime.strptime(s, "%y%m%d %H%M%S")


def main() -> None:
    with find("anomaly_label.csv").open() as f:
        anomalous = {r["BlockId"] for r in csv.DictReader(f) if r["Label"] == "Anomaly"}
    base = {int(t): v for t, v in json.loads(BASELINE.read_text()).items()}
    share = {t: v["block_share"] for t, v in base.items()}
    rare = {t for t, s in share.items() if s < RARE}
    lifecycle = {t for t, s in share.items() if s >= EXPECTED}

    miner = build_miner(MODEL)
    tids: dict[str, set[int]] = defaultdict(set)
    first_seen: dict[str, str] = {}
    rare_first: dict[str, dict[int, str]] = defaultdict(dict)
    start = time.time()
    with find("HDFS.log").open(errors="replace") as f:
        for n, line in enumerate(f, 1):
            parts = line.rstrip("\n").split(" ", 5)
            if len(parts) < 6:
                continue
            cluster = miner.match(parts[5], full_search_strategy="fallback")
            if cluster is None:
                continue
            t, when = cluster.cluster_id, line[:13]
            for blk in set(BLK.findall(parts[5])):
                tids[blk].add(t)
                first_seen.setdefault(blk, when)
                if t in rare:
                    rare_first[blk].setdefault(t, when)
            if n % 2_000_000 == 0:
                print(f"  {n:,} lines  {n / (time.time() - start):,.0f}/s", flush=True)

    with_t: Counter = Counter()
    bad_t: Counter = Counter()
    for b, ts_ in tids.items():
        for t in ts_:
            with_t[t] += 1
            bad_t[t] += b in anomalous
    strong = {t for t in rare if bad_t[t] >= MIN_ANOMALOUS and bad_t[t] / with_t[t] >= STRONG_RATIO}
    weak = {t for t in rare if t not in strong and bad_t[t] >= MIN_ANOMALOUS
            and bad_t[t] / with_t[t] >= WEAK_RATIO}
    print(f"\nstrong signatures: {sorted(strong)}\nweak signatures:   {sorted(weak)}")

    rng = random.Random(SEED)

    # anomalous with a strong signature, grouped by the signature that appears first,
    # then picked round-robin so the set mixes failure types
    by_sig: dict[int, list[str]] = defaultdict(list)
    for b in sorted(anomalous):
        s = tids.get(b, set()) & strong
        if s:
            by_sig[min(s, key=lambda t: rare_first[b][t])].append(b)
    for lst in by_sig.values():
        rng.shuffle(lst)
    order = sorted(by_sig, key=lambda t: (-len(by_sig[t]), t))
    strong_picks: list[str] = []
    while len(strong_picks) < N_STRONG and any(by_sig.values()):
        for t in order:
            if by_sig[t] and len(strong_picks) < N_STRONG:
                strong_picks.append(by_sig[t].pop())

    missing_only = sorted(b for b in anomalous if b in tids and not tids[b] & rare
                          and tids[b] & lifecycle and lifecycle - tids[b])
    normal_repl = sorted(b for b, t in tids.items() if b not in anomalous and t & weak and not t & strong)
    normal_plain = sorted(b for b, t in tids.items() if b not in anomalous and not t & rare and lifecycle <= t)
    print(f"pools: strong {sum(len(v) for v in by_sig.values()) + len(strong_picks):,}  "
          f"missing-only {len(missing_only):,}  normal+replication {len(normal_repl):,}  "
          f"normal plain {len(normal_plain):,}")

    def item(b: str, label: str, kind: str, when: str, expected: set[int]) -> dict:
        t = ts(when)
        return {
            "block_id": b, "label": label, "kind": kind,
            "ticket_time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ticket": f"Jobs writing to HDFS reported problems with block {b} around "
                      f"{t:%Y-%m-%d %H:%M} UTC. Please investigate.",
            "expected_templates": sorted(expected),           # scoring only
            "missing_templates": sorted(lifecycle - tids[b]),  # scoring only
            "rare_templates": sorted(tids[b] & rare),          # scoring only
        }

    items = []
    for b in strong_picks:
        sig = tids[b] & strong
        first = min(sig, key=lambda t: rare_first[b][t])
        items.append(item(b, "anomalous", f"strong:T{first}", rare_first[b][first], sig))
    for b in rng.sample(missing_only, N_MISSING):
        items.append(item(b, "anomalous", "missing", first_seen[b], set()))
    for b in rng.sample(normal_repl, N_REPL):
        w = tids[b] & weak
        items.append(item(b, "normal", "replication", min(rare_first[b][t] for t in w), set()))
    for b in rng.sample(normal_plain, N_PLAIN):
        items.append(item(b, "normal", "plain", first_seen[b], set()))

    rng.shuffle(items)  # so labels aren't grouped
    for i, it in enumerate(items, 1):
        it["id"] = f"L{i:02d}"

    OUT.write_text(json.dumps({
        "seed": SEED, "strong": sorted(strong), "weak": sorted(weak),
        "lifecycle": sorted(lifecycle), "items": items,
    }, indent=2))

    print(f"\n{'id':<4} {'label':<10} {'kind':<12} {'time':<21} rare / missing")
    for it in items:
        print(f"{it['id']:<4} {it['label']:<10} {it['kind']:<12} {it['ticket_time']:<21} "
              f"{it['rare_templates']} / {it['missing_templates']}")
    print(f"\nsaved {OUT}")


if __name__ == "__main__":
    main()