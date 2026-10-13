"""Load HDFS v1 logs into Loki with labels component and level, then flush to storage.

Wipe-and-reload rule: Loki has no delete, and pushing the same file twice stores every line twice
(same timestamps, but out-of-order duplicates are accepted per stream). To reload, stop Loki, remove
the loki-data volume (`docker compose down loki && docker volume rm triagerag_loki-data`), start it,
then run load_logs again."""
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx

from triagerag.config import settings
from triagerag.jobs.events import step
from triagerag.jobs.registry import job

BATCH = 5_000


class LokiPushError(RuntimeError):
    pass


def find_log() -> Path:
    hits = list(settings.data_dir.rglob("HDFS.log"))
    if not hits:
        raise FileNotFoundError(f"HDFS.log not found under {settings.data_dir}")
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


def push(client: httpx.Client, url: str, batch: dict, errors: list[str]) -> None:
    streams = [
        {"stream": {"component": c, "level": lvl}, "values": values}
        for (c, lvl), values in batch.items()
    ]
    for attempt in range(1, 6):
        r = client.post(f"{url}/loki/api/v1/push", json={"streams": streams})
        if r.status_code == 204:
            return
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(2 * attempt)
            continue
        errors.append(f"{r.status_code}: {r.text[:300]}")  # 400: Loki rejected some lines, kept the rest
        return
    raise LokiPushError("push failed after 5 attempts")


def load(path: Path | None = None, limit: int = 0, url: str | None = None,
         on_batch: Callable[[int], None] | None = None) -> dict:
    """Push the log file to Loki. on_batch(lines_so_far) runs after every pushed batch."""
    log = path or find_log()
    url = url or settings.loki_url
    per_second: dict[int, int] = defaultdict(int)
    streams: Counter = Counter()
    errors: list[str] = []
    skipped = n = 0
    batch: dict = defaultdict(list)
    n_batch = 0
    start = time.time()

    with httpx.Client(timeout=60) as client, log.open(errors="replace") as f:
        for n, raw in enumerate(f, 1):
            if limit and n > limit:
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
                push(client, url, batch, errors)
                batch, n_batch = defaultdict(list), 0
                if on_batch:
                    on_batch(n)

        if batch:
            push(client, url, batch, errors)
        client.post(f"{url}/flush")

    return {"lines": sum(streams.values()), "skipped": skipped, "seconds": round(time.time() - start),
            "streams": {f"{c} {lvl}": k for (c, lvl), k in streams.most_common()},
            "push_errors": len(errors), "first_errors": errors[:3]}


# not idempotent: a retry would duplicate the lines already pushed
@job("load_logs", max_attempts=1, stages=("push",))
def load_logs(ctx, payload: dict) -> dict:
    path = Path(payload["path"]) if payload.get("path") else None
    with step("push", {"path": str(path or find_log()), "batch": BATCH}) as st:
        def on_batch(n: int) -> None:
            ctx.heartbeat()
            st.progress({"lines_read": n})
        result = load(path, limit=int(payload.get("limit") or 0), on_batch=on_batch)
        st.output = {k: v for k, v in result.items() if k != "streams"}
    result.pop("streams")
    return result
