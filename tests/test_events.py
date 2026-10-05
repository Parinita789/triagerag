from conftest import StubLLM, make_issue
from triagerag.jobs import events, queue
from triagerag.jobs.worker import Resources, work
from triagerag.pipeline import index

SECRET = "AKIA1234567890ABCDEF"


def test_record_step_is_noop_outside_a_job(db):
    events.record_step("llm", output={"x": 1})
    assert db.execute("select count(*) from job_events").fetchone()[0] == 0


def test_cap_redacts_and_limits_size():
    out = events.cap({"text": f"token {SECRET} " + "x" * 50_000, "items": ["y" * 9_000] * 3})
    text = str(out)
    assert SECRET not in text and len(text) < 9_000


def test_triage_events_in_order_and_redacted(db):
    for key, summary in (("HDFS-9101", "DataNode block report rejected after restart"),
                         ("HDFS-9102", "Balancer ignores exclude file")):
        index.upsert_ticket(db, make_issue(key, summary, summary + " in detail."))
    index.index_keys(db, ["HDFS-9101", "HDFS-9102"])
    resources = Resources()
    resources.__dict__["llm"] = StubLLM()

    # enqueued raw on purpose: the handler must still never let the secret reach job_events
    job_id = queue.enqueue(db, "triage_ticket", {
        "summary": "Block report rejected", "description": f"creds: {SECRET} in hdfs-site.xml"})
    work(db, ["triage_ticket"], resources, once=True)

    rows = db.execute("select seq, step, status, input::text, output::text from job_events "
                      "where job_id = %s order by seq", (job_id,)).fetchall()
    steps = [r[1] for r in rows]
    assert 8 <= len(rows) <= 12, steps
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
    assert steps[0] == "redact" and steps[-1] == "comment"
    assert {"retrieve.dense", "retrieve.bm25", "rrf", "llm", "tool", "validate"} <= set(steps)
    assert not any(SECRET in (r[3] or "") or SECRET in (r[4] or "") for r in rows)
    assert '"AWS_KEY": 1' in rows[0][4]
