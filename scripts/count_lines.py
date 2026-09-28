from pathlib import Path
from loginsights.ingest.reader import iter_lines

LOG = Path("data/HDFS.log")

def main() -> None:
    parsed = unparsed = no_block = 0
    unparsed_samples: list[str] = []

    for n, p in iter_lines(LOG):
        if p is None:
            unparsed += 1
            if len(unparsed_samples) < 20:
                unparsed_samples.append(f"line {n}")
        else:
            parsed += 1
            if not p.block_ids:
                no_block += 1
        if n % 1_000_000 == 0:
            print(f"  ...{n:,}")
    print(f"parsed:   {parsed:,}")
    print(f"unparsed: {unparsed:,}")
    print(f"no block: {no_block:,}")
    print(f"total:    {parsed + unparsed:,}")
    print("unparsed at:", unparsed_samples[:20])


if __name__ == "__main__":
    main()