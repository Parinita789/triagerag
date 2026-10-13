from collections import Counter

import psycopg

from triagerag.config import settings
from triagerag.shared.clean import clean_ticket
from triagerag.shared.extract import EXC_LINE_RE, log_messages

CUTOFF = "2018-12-06"


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute(
            "select raw, resolved_at < %s as train from tickets", (CUTOFF,)
        ).fetchall()

    stats: Counter[str] = Counter()
    for issue, train in rows:
        t = clean_ticket(issue)
        texts = [t.description] + [c.text for c in t.comments]
        n_logs = n_exc = 0
        for text in texts:
            for source, _ in log_messages(text):
                n_logs += 1
                stats[f"logs_in_{source}"] += 1
            for body in text.blocks + [text.prose]:
                n_exc += sum(1 for line in body.splitlines() if EXC_LINE_RE.match(line))
        stats["log_lines"] += n_logs
        stats["exception_lines"] += n_exc
        stats["tickets_with_logs"] += n_logs > 0
        stats["tickets_with_exceptions"] += n_exc > 0
        if train:
            stats["train_log_lines"] += n_logs

    print(f"tickets: {len(rows):,}")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")


if __name__ == "__main__":
    main()