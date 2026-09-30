import random
from collections import defaultdict
from triagerag.shared.redact import find_spans

import psycopg

from triagerag.config import settings
from triagerag.shared.redact import DETECTORS, _luhn_ok

CONTEXT = 40
SAMPLES = 10


def main() -> None:
    with psycopg.connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("select key, description from tickets where description is not null")
            rows = cur.fetchall()

    hits: dict[str, list[str]] = defaultdict(list)
    # for key, text in rows:
    #     for det in DETECTORS:
    #         for m in det.pattern.finditer(text):
    #             start, end = m.span(det.group)
    #             value = m.group(det.group)
    #             if det.kind == "CARD" and not _luhn_ok(value):
    #                 continue
    #             before = text[max(0, start - CONTEXT):start].replace("\n", " ")
    #             after = text[end:end + CONTEXT].replace("\n", " ")
    #             hits[det.kind].append(f"{key}: ...{before}[[{value}]]{after}...")


    for key, text in rows:
        for start, end, kind in find_spans(text):
            value = text[start:end]
            before = text[max(0, start - CONTEXT):start].replace("\n", " ")
            after = text[end:end + CONTEXT].replace("\n", " ")
            hits[kind].append(f"{key}: ...{before}[[{value}]]{after}...")

    rng = random.Random(42)
    for kind, examples in hits.items():
        print(f"\n=== {kind} ({len(examples)} total, showing {min(SAMPLES, len(examples))}) ===")
        for ex in rng.sample(examples, min(SAMPLES, len(examples))):
            print(" ", ex)


if __name__ == "__main__":
    main()