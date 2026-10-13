import pytest
from conftest import StubLLM, make_issue
from fastapi.testclient import TestClient

from triagerag.jobs import queue
from triagerag.jobs.worker import Resources, work
from triagerag.pipeline import index, triage
from triagerag.service import app as service

KEY = "test-api-key"
H = {"X-API-Key": KEY}


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setattr(service.settings, "api_key", KEY)
    return TestClient(service.app)


def test_requires_api_key(client):
    assert client.get("/jobs").status_code == 401
    assert client.get("/jobs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/jobs", headers=H).status_code == 200


def test_unknown_type_is_400(client):
    assert client.post("/jobs", json={"type": "nope"}, headers=H).status_code == 400


def test_enqueue_redacts_payload_and_lists(client, db):
    r = client.post("/jobs", headers=H, json={"type": "triage_ticket", "payload": {
        "summary": "dn down", "description": "key AKIA1234567890ABCDEF"}})
    assert r.status_code == 202
    job_id = r.json()["id"]
    payload = db.execute("select payload from jobs where id = %s", (job_id,)).fetchone()[0]
    assert "AKIA" not in str(payload) and payload["redactions"] == {"AWS_KEY": 1}
    jobs = client.get("/jobs?status=queued&type=triage_ticket", headers=H).json()
    assert [j["id"] for j in jobs] == [job_id]
    assert client.get("/metrics/jobs", headers=H).json() == {"triage_ticket": {"queued": 1}}


def test_retry_failed_job(client, db):
    job_id = queue.enqueue(db, "refresh_bm25", {}, max_attempts=1)
    assert client.post(f"/jobs/{job_id}/retry", headers=H).status_code == 409  # not failed yet
    db.execute("update jobs set status = 'failed' where id = %s", (job_id,))
    assert client.post(f"/jobs/{job_id}/retry", headers=H).status_code == 200
    assert client.get(f"/jobs/{job_id}", headers=H).json()["status"] == "queued"


def test_stream_sends_steps_and_ends(client, db):
    for key in ("HDFS-9201", "HDFS-9202"):
        index.upsert_ticket(db, make_issue(key, f"{key} datanode restart", "block report rejected"))
    index.index_keys(db, ["HDFS-9201", "HDFS-9202"])
    job_id = client.post("/jobs", headers=H, json={"type": "triage_ticket", "payload": {
        "summary": "block report rejected after restart"}}).json()["id"]
    resources = Resources()
    resources.__dict__["llm"] = StubLLM()
    work(db, ["triage_ticket"], resources, once=True)

    with client.stream("GET", f"/jobs/{job_id}/stream?key={KEY}") as r:
        body = "".join(r.iter_text())
    assert body.count("event: step") >= len(triage.STAGES)
    assert "event: end" in body and '"status": "done"' in body

    detail = client.get(f"/jobs/{job_id}", headers=H).json()
    assert detail["stages"] == list(triage.STAGES)
    listed = client.get("/jobs", headers=H).json()[0]
    assert listed["steps"]["llm"] == "ok" and listed["steps"]["rerank"] == "ok"
    assert detail["triage"]["issue_key"] == f"TRY-{job_id}"
    assert [e["seq"] for e in detail["events"]] == list(range(1, len(detail["events"]) + 1))
