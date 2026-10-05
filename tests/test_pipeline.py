import pytest

from conftest import StubLLM, make_issue
from triagerag.jobs import queue
from triagerag.jobs.worker import Resources, work
from triagerag.pipeline import index

ALL = ["triage_ticket", "index_ticket", "index_batch", "reindex_all", "refresh_bm25"]

TICKETS = {
    "HDFS-9001": make_issue("HDFS-9001", "NameNode web UI slow with many snapshots",
                            "Rendering the snapshot page takes minutes when there are 10k snapshots.",
                            ["Caching the snapshot list in the servlet fixes the page load time."]),
    "HDFS-9002": make_issue("HDFS-9002", "Balancer ignores excluded hosts file",
                            "The balancer still moves blocks to hosts listed in the exclude file."),
    "HDFS-9003": make_issue("HDFS-9003", "DataNode block report fails after restart with stale storage",
                            "After restarting a DataNode its full block report is rejected because "
                            "storage UUIDs changed; the NameNode marks blocks missing.",
                            ["The storage UUID is regenerated on format; keep it in VERSION."]),
}


@pytest.fixture
def fake_jira(monkeypatch):
    monkeypatch.setattr(index, "fetch_issue", lambda key: TICKETS[key])


def run_all(db, resources: Resources) -> None:
    db.execute("update jobs set run_after = now() where status = 'queued'")  # skip delays
    while db.execute("select 1 from jobs where status = 'queued'").fetchone():
        work(db, ALL, resources, once=True)
        db.execute("update jobs set run_after = now() where status = 'queued'")


def test_index_same_ticket_twice_no_duplicates(db, fake_jira):
    for _ in range(2):
        queue.enqueue(db, "index_ticket", {"key": "HDFS-9003"})
        run_all(db, Resources())
    n, distinct = db.execute(
        "select count(*), count(distinct (section, content)) from chunks where ticket_key = 'HDFS-9003'"
    ).fetchone()
    assert n == distinct >= 2
    assert db.execute("select count(*) from jobs where status = 'failed'").fetchone()[0] == 0


def test_reindex_all_fans_out(db, monkeypatch):
    for key, issue in TICKETS.items():
        index.upsert_ticket(db, issue)
    monkeypatch.setattr(index, "TICKETS_PER_BATCH", 2)
    parent = queue.enqueue(db, "reindex_all", {"limit": 3})
    run_all(db, Resources())
    children = db.execute("select status from jobs where parent_id = %s", (parent,)).fetchall()
    assert [s for (s,) in children] == ["done", "done"]
    assert db.execute("select count(distinct ticket_key) from chunks").fetchone()[0] == 3
    assert db.execute("select bm25_version from index_state").fetchone()[0] >= 1


def test_triage_sees_ticket_indexed_after_startup(db, fake_jira):
    for key in ("HDFS-9001", "HDFS-9002"):
        index.upsert_ticket(db, TICKETS[key])
    index.index_keys(db, ["HDFS-9001", "HDFS-9002"])
    resources = Resources()
    resources.__dict__["llm"] = StubLLM()
    assert "HDFS-9003" not in set(resources.search.bm25.tickets)  # built before the new ticket

    queue.enqueue(db, "index_ticket", {"key": "HDFS-9003"})
    run_all(db, resources)  # index_ticket, then refresh_bm25
    job_id = queue.enqueue(db, "triage_ticket", {
        "summary": "DataNode block report rejected after restart",
        "description": "stale storage UUID, NameNode marks blocks missing"})
    run_all(db, resources)

    assert "HDFS-9003" in set(resources.search.bm25.tickets)
    status, result = db.execute("select status, result from jobs where id = %s", (job_id,)).fetchone()
    assert status == "done" and result["related"][0] == "HDFS-9003"
    comment = db.execute("select comment from triages where issue_key = %s", (f"TRY-{job_id}",)).fetchone()[0]
    assert "HDFS-9003" in comment
