import argparse
import math
from collections import Counter

import psycopg

from triagerag.config import settings
from triagerag.index.logs.drain import DEFAULT_STATE_PATH, build_miner
from triagerag.shared.clean import clean_ticket
from triagerag.shared.extract import log_messages

CUTOFF = "2018-12-06"
RARE_DF = 20  # a template seen in at most this many training tickets counts as informative


def ticket_messages(issue: dict) -> list[str]:
    t = clean_ticket(issue)
    return [msg for text in [t.description] + [c.text for c in t.comments]
            for _, msg in log_messages(text)]


def main() -> None:
    ap = argparse.ArgumentParser(description="Train Drain on log lines pasted into tickets.")
    ap.add_argument("--sim-th", type=float, default=0.75)
    ap.add_argument("--no-save", action="store_true", help="experiment only: don't write model or DB")
    args = ap.parse_args()

    with psycopg.connect(settings.database_url) as conn:
        train_rows = conn.execute("select raw from tickets where resolved_at < %s", (CUTOFF,)).fetchall()
        test_rows = conn.execute("select raw from tickets where created_at >= %s", (CUTOFF,)).fetchall()

    train_per_ticket = [ticket_messages(issue) for (issue,) in train_rows]
    test_msgs = [m for (issue,) in test_rows for m in ticket_messages(issue)]

    DEFAULT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_save:
        DEFAULT_STATE_PATH.unlink(missing_ok=True)
    miner = build_miner(None if args.no_save else DEFAULT_STATE_PATH)
    miner.drain.sim_th = args.sim_th

    for msgs in train_per_ticket:
        for msg in msgs:
            miner.add_log_message(msg)

    clusters = sorted(miner.drain.clusters, key=lambda c: c.size, reverse=True)
    n_train_lines = sum(len(m) for m in train_per_ticket)
    singletons = sum(1 for c in clusters if c.size == 1)
    print(f"sim_th={args.sim_th}: {n_train_lines:,} lines → {len(clusters)} templates, "
          f"{singletons} seen once ({singletons / len(clusters):.0%})")

    print("\ntop 15 by size:")
    for c in clusters[:15]:
        print(f"  [{c.cluster_id}] size={c.size} | {c.get_template()[:110]}")

    df: Counter[int] = Counter()
    for msgs in train_per_ticket:
        ids = {c.cluster_id for m in msgs if (c := miner.match(m))}
        df.update(ids)

    n_tickets = len(train_rows)
    by_id = {c.cluster_id: c.get_template() for c in clusters}
    print("\nmost widespread templates (lowest information):")
    for tid, count in df.most_common(10):
        print(f"  in {count} tickets, idf={math.log(n_tickets / count):.1f} | {by_id[tid][:90]}")

    matched = [c for m in test_msgs if (c := miner.match(m))]
    informative = [c for c in matched if df[c.cluster_id] <= RARE_DF]
    total = max(len(test_msgs), 1)
    print(f"\ntest log lines: {len(test_msgs):,}")
    print(f"  match any template:          {len(matched):,} ({len(matched) / total:.0%})")
    print(f"  match a rare template (≤{RARE_DF}): {len(informative):,} ({len(informative) / total:.0%})")

    if args.no_save:
        return

    miner.save_state("trained on ticket logs")
    with psycopg.connect(settings.database_url) as conn:
        conn.execute("delete from templates")
        conn.cursor().executemany(
            "insert into templates (template_id, template, size) values (%s, %s, %s)",
            [(c.cluster_id, c.get_template(), c.size) for c in clusters],
        )
        conn.commit()
    print(f"\nsaved model to {DEFAULT_STATE_PATH} and {len(clusters)} templates to postgres")


if __name__ == "__main__":
    main()