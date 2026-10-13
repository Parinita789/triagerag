import re
from collections import Counter

import psycopg

from triagerag.config import settings
from triagerag.shared.clean import clean_ticket

LEFTOVERS = {
    "monospace {{": re.compile(r"\{\{"),
    "code tag": re.compile(r"\{(?:code|noformat)"),
    "image !x!": re.compile(r"![^!\s]+\.(?:png|jpg|gif)"),
    "link [x|y]": re.compile(r"\[[^\]\n]+\|[^\]\n]+\]"),
    "heading h2.": re.compile(r"^h[1-6]\.", re.M),
}


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        rows = conn.execute("select raw from tickets").fetchall()

    stats: Counter[str] = Counter()
    leftovers: Counter[str] = Counter()
    examples: dict[str, str] = {}

    for (issue,) in rows:
        t = clean_ticket(issue)
        stats["tickets"] += 1
        stats["blocks"] += len(t.description.blocks) + sum(len(c.text.blocks) for c in t.comments)
        stats["comments_kept"] += len(t.comments)
        stats["bot_comments_dropped"] += t.bot_comments_dropped
        stats.update({f"redacted_{k}": v for k, v in t.redactions.items()})

        for text in [t.description.prose] + [c.text.prose for c in t.comments]:
            for name, pattern in LEFTOVERS.items():
                m = pattern.search(text)
                if m:
                    leftovers[name] += 1
                    examples.setdefault(name, f"{t.key}: ...{text[max(0, m.start() - 60):m.end() + 60]}...")

    print("stats:")
    for k, v in stats.items():
        print(f"  {k}: {v:,}")
    print("\nleftover markup in prose:")
    for name, n in leftovers.most_common():
        print(f"  {name}: {n}")
        print(f"    e.g. {examples[name]}")


if __name__ == "__main__":
    main()