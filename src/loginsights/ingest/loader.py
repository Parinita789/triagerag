import csv
from pathlib import Path
import psycopg
import random
from loginsights.config import settings
from loginsights.ingest.reader import iter_lines

def load_labels(path: Path) -> int:
    """Read anomaly_label.csv and bulk-insert into blocks. Returns row count."""
    ...
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append((row["BlockId"], row["Label"]))

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            with cur.copy("COPY blocks (block_id, label) FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
                    
    return len(rows)
 

def sample_blocks(n: int = 5000, seed: int = 42) -> int:
    """Stratified sample of n blocks preserving anomaly ratio.
    Returns count of blocks marked in_sample."""

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:

            # 1. fetch all block IDs grouped by label
            cur.execute("select block_id, label from blocks")
            rows = cur.fetchall()

    # 2. split by class
    anomaly_ids = [r[0] for r in rows if r[1] == "Anomaly"]
    normal_ids  = [r[0] for r in rows if r[1] == "Normal"]

    # 3. proportional counts
    total = len(rows)
    n_anomaly = round(n * len(anomaly_ids) / total)
    n_normal  = n - n_anomaly

    # 4. sample with fixed seed
    rng = random.Random(seed)
    sampled = (
        rng.sample(anomaly_ids, n_anomaly) +
        rng.sample(normal_ids, n_normal)
    )

    # 5. mark in_sample
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "update blocks set in_sample = true where block_id = any(%s)",
                (sampled,)
            )
        conn.commit()

    return len(sampled)


def load_log_lines(log_path: Path, batch_size: int = 5000) -> int:
    """Stream HDFS.log, insert lines for sampled blocks only.
    Returns total lines inserted."""

    # fetch sampled block IDs into a set — O(1) lookup
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("select block_id from blocks where in_sample = true")
            sampled = {r[0] for r in cur.fetchall()}

    inserted = 0
    batch: list[tuple] = []

    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            for line_number, parsed in iter_lines(log_path):
                if parsed is None:
                    continue
                for block_id in parsed.block_ids:
                    if block_id not in sampled:
                        continue
                    batch.append((
                        block_id,
                        parsed.ts,
                        parsed.pid,
                        parsed.level,
                        parsed.component,
                        parsed.message,
                        line_number,
                    ))
                    if len(batch) >= batch_size:
                        with cur.copy(
                            "COPY log_lines (block_id, ts, pid, level, component, message, line_number) FROM STDIN"
                        ) as copy:
                            for row in batch:
                                copy.write_row(row)
                        inserted += len(batch)
                        batch.clear()
                        print(f"  inserted {inserted:,}")

            # flush remainder
            if batch:
                with cur.copy(
                    "COPY log_lines (block_id, ts, pid, level, component, message, line_number) FROM STDIN"
                ) as copy:
                    for row in batch:
                        copy.write_row(row)
                inserted += len(batch)

        conn.commit()

    return inserted