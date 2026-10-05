"""Load HDFS v1 logs into Loki. Same code as the load_logs job; see pipeline/logs.py for the reload rule."""
import argparse

from triagerag.pipeline.logs import load


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="load only the first N lines")
    args = ap.parse_args()

    last = [0]

    def progress(n: int) -> None:
        if n // 1_000_000 > last[0]:
            last[0] = n // 1_000_000
            print(f"  {n:,} lines", flush=True)

    r = load(limit=args.limit, on_batch=progress)
    print(f"\nloaded {r['lines']:,} lines in {r['seconds']:,}s, skipped {r['skipped']}")
    print(f"streams: {len(r['streams'])}")
    for name, count in r["streams"].items():
        print(f"  {name:<47} {count:>10,}")
    print(f"push errors: {r['push_errors']}")
    for e in r["first_errors"]:
        print("  ", e)


if __name__ == "__main__":
    main()
