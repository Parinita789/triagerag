import threading

import psycopg
import pytest

from triagerag.jobs import queue, registry
from triagerag.jobs.worker import Resources, run_one


@registry.job("test_ok")
def _ok(ctx, payload):
    return {"echo": payload}


@registry.job("test_boom", max_attempts=2)
def _boom(ctx, payload):
    raise RuntimeError("boom")


def test_two_threads_claim_one_job_once(db, test_db_url):
    queue.enqueue(db, "test_ok", {})
    claimed, barrier = [], threading.Barrier(2)

    def worker():
        with psycopg.connect(test_db_url, autocommit=True) as conn:
            barrier.wait()
            claimed.append(queue.claim(conn, ["test_ok"]))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([j for j in claimed if j is not None]) == 1


def test_failure_backs_off_then_fails(db):
    job_id = queue.enqueue(db, "test_boom", {})
    job = queue.claim(db, ["test_boom"])
    assert run_one(db, job, Resources()) == "queued"
    status, run_after_future, error = db.execute(
        "select status, run_after > now() + interval '20 seconds', error from jobs where id = %s",
        (job_id,)).fetchone()
    assert status == "queued" and run_after_future and error.startswith("RuntimeError: boom")
    assert queue.claim(db, ["test_boom"]) is None  # backing off

    db.execute("update jobs set run_after = now() where id = %s", (job_id,))
    job = queue.claim(db, ["test_boom"])
    assert job.attempts == 2
    assert run_one(db, job, Resources()) == "failed"
    assert db.execute("select finished_at is not null from jobs where id = %s", (job_id,)).fetchone()[0]


def test_dedupe_only_while_active(db):
    first = queue.enqueue(db, "test_ok", {}, dedupe_key="bm25")
    assert queue.enqueue(db, "test_ok", {}, dedupe_key="bm25") == first
    assert db.execute("select count(*) from jobs").fetchone()[0] == 1

    job = queue.claim(db, ["test_ok"])
    assert queue.enqueue(db, "test_ok", {}, dedupe_key="bm25") == first  # running still counts
    queue.complete(db, job.id, {})
    assert queue.enqueue(db, "test_ok", {}, dedupe_key="bm25") != first


def test_stale_heartbeat_requeued_fresh_not(db):
    stale = queue.enqueue(db, "test_ok", {})
    fresh = queue.enqueue(db, "test_ok", {})
    queue.claim(db, ["test_ok"])
    queue.claim(db, ["test_ok"])
    db.execute("update jobs set heartbeat_at = now() - interval '6 minutes' where id = %s", (stale,))
    assert queue.requeue_stale(db, minutes=5) == 1
    rows = dict(db.execute("select id, status from jobs").fetchall())
    assert rows == {stale: "queued", fresh: "running"}


def test_unknown_type_rejected(db):
    with pytest.raises(registry.UnknownJobType):
        queue.enqueue(db, "no_such_type", {})
    assert db.execute("select count(*) from jobs").fetchone()[0] == 0


def test_future_run_after_not_claimed(db):
    queue.enqueue(db, "test_ok", {}, delay=60)
    assert queue.claim(db, ["test_ok"]) is None


def test_retry_resets_failed(db):
    job_id = queue.enqueue(db, "test_boom", {}, max_attempts=1)
    run_one(db, queue.claim(db, ["test_boom"]), Resources())
    assert queue.retry(db, job_id)
    assert db.execute("select status, attempts from jobs where id = %s", (job_id,)).fetchone() == ("queued", 0)
