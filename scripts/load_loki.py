"""Load HDFS v1 logs into Loki with labels component and level, then flush to storage."""
import argparse
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx

LOKI = "http://localhost:3100"
BATCH = 5_000


def find_log() -> Path:
    hits = list(Path("data").rglob("HDFS.log"))
    if not hits:
        sys.exit("HDFS.log not found under data/")
    return hits[0]


def parse(line: str) -> tuple[int, str, str] | None:
    """'081109 203518 143 INFO dfs.DataNode$DataXceiver: msg' -> (epoch_s, level, component)."""
    parts = line.split(" ", 5)
    if len(parts) < 6:
        return None
    date, clock, _thread, level, component, _msg = parts
    try:
        dt = datetime.strptime(date + clock, "%y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return int(dt.timestamp()), level, component.rstrip(":")


def push(client: httpx.Client, batch: dict, errors: list[str]) -> None:
    streams = [
        {"stream": {"component": c, "level": lvl}, "values": values}
        for (c, lvl), values in batch.items()
    ]
    for attempt in range(1, 6):
        r = client.post(f"{LOKI}/loki/api/v1/push", json={"streams": streams})
        if r.status_code == 204:
            return
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * attempt)
            continue
        errors.append(f"{r.status_code}: {r.text[:300]}")  # 400: Loki rejected some lines, kept the rest
        return
    sys.exit("push failed after 5 attempts")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="load only the first N lines")
    args = ap.parse_args()

    log = find_log()
    per_second: dict[int, int] = defaultdict(int)
    streams: Counter = Counter()
    errors: list[str] = []
    skipped = 0
    batch: dict = defaultdict(list)
    n_batch = 0
    start = time.time()

    with httpx.Client(timeout=60) as client, log.open(errors="replace") as f:
        for n, raw in enumerate(f, 1):
            if args.limit and n > args.limit:
                break
            line = raw.rstrip("\n")
            parsed = parse(line)
            if parsed is None:
                skipped += 1
                continue
            sec, level, component = parsed
            # unique, stable nanosecond timestamp: lines in the same second keep file order,
            # and identical lines in the same second aren't dropped as duplicates
            ns = sec * 1_000_000_000 + per_second[sec]
            per_second[sec] += 1

            batch[(component, level)].append([str(ns), line])
            streams[(component, level)] += 1
            n_batch += 1
            if n_batch >= BATCH:
                push(client, batch, errors)
                batch, n_batch = defaultdict(list), 0
            if n % 1_000_000 == 0:
                print(f"  {n:,} lines  {n / (time.time() - start):,.0f}/s", flush=True)

        if batch:
            push(client, batch, errors)

        print("flushing ingester to storage...", flush=True)
        client.post(f"{LOKI}/flush")

    print(f"\nloaded {sum(streams.values()):,} lines in {time.time() - start:,.0f}s, skipped {skipped}")
    print(f"streams: {len(streams)}")
    for (c, lvl), count in streams.most_common():
        print(f"  {c:<40} {lvl:<6} {count:>10,}")
    print(f"push errors: {len(errors)}")
    for e in errors[:3]:
        print("  ", e)


if __name__ == "__main__":
    main()