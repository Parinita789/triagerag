from conftest import StubLLM, make_issue

from triagerag.jobs import events, queue
from triagerag.jobs.worker import Resources, work
from triagerag.pipeline import index, triage

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
    assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
    assert all(r[2] == "ok" for r in rows), rows  # nothing left 'running'
    # the lifecycle, in order (tool calls interleave but aren't stages)
    stages = [s for s in steps if s in triage.STAGES]
    assert stages == list(triage.STAGES), steps
    assert not any(SECRET in (r[3] or "") or SECRET in (r[4] or "") for r in rows)
    redact_row = rows[steps.index("redact")]
    assert '"AWS_KEY": 1' in redact_row[4]


def test_step_is_running_until_it_ends(db):
    job_id = queue.enqueue(db, "refresh_bm25", {})
    token = events.current_job.set(job_id)
    try:
        with events.step("slow", {"n": 1}) as st:
            assert db.execute("select status, ms from job_events where job_id = %s",
                              (job_id,)).fetchone() == ("running", None)
            st.progress({"done": 1})
            st.output = {"done": 2}
        status, ms, output = db.execute(
            "select status, ms, output from job_events where job_id = %s", (job_id,)).fetchone()
        assert status == "ok" and ms is not None and output == {"done": 2}

        try:
            with events.step("boom"):
                raise ValueError("nope")
        except ValueError:
            pass
        assert db.execute("select status, output->>'error' from job_events where step = 'boom'"
                          ).fetchone() == ("error", "ValueError: nope")
    finally:
        events.current_job.reset(token)
