import argparse
from pathlib import Path

import psycopg
from drain3 import TemplateMiner

from triagerag.config import settings
from triagerag.index.logs.reader import iter_lines
from triagerag.index.logs.drain import DEFAULT_STATE_PATH, build_miner, load_miner


def save_templates(miner: TemplateMiner) -> int:
    """Upsert every Drain cluster into the templates table. Returns count written."""
    clusters = miner.drain.clusters
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """insert into templates (template_id, template, size)
                   values (%s, %s, %s)
                   on conflict (template_id) do update
                   set template = excluded.template, size = excluded.size""",
                [(c.cluster_id, c.get_template(), c.size) for c in clusters],
            )
        conn.commit()
    return len(clusters)


def main() -> None:
    ap = argparse.ArgumentParser(description="Train Drain templates on the HDFS log.")
    ap.add_argument("--retrain", action="store_true", help="discard saved state and retrain")
    ap.add_argument("--log", type=Path, default=Path("data/HDFS.log"), help="log file to train on")
    args = ap.parse_args()

    if DEFAULT_STATE_PATH.exists() and not args.retrain:
        miner = load_miner()
        print(f"loaded saved state from {DEFAULT_STATE_PATH} (use --retrain to rebuild)")
    else:
        DEFAULT_STATE_PATH.unlink(missing_ok=True)
        miner = build_miner(DEFAULT_STATE_PATH)
        print("sim_th =", miner.drain.sim_th)

        for n, parsed in iter_lines(args.log):
            if parsed is None:
                continue
            miner.add_log_message(parsed.message)
            if n % 1_000_000 == 0:
                print(f"  {n:,} lines — {len(miner.drain.clusters)} templates so far")

        miner.save_state("training complete")
        print(f"saved state to {DEFAULT_STATE_PATH}")

    print(f"\nfinal template count: {len(miner.drain.clusters)}")
    for cluster in miner.drain.clusters:
        print(f"  [{cluster.cluster_id}] size={cluster.size:,} | {cluster.get_template()}")

    written = save_templates(miner)
    print(f"\nwrote {written} templates to postgres")


if __name__ == "__main__":
    main()
