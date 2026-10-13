import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from triagerag.service import app as service
from triagerag.service.app import adf_text, verify_signature

SECRET = "test-secret"


def sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def test_good_signature_passes():
    verify_signature(b"{}", sign(b"{}"), SECRET)


def test_tampered_body_rejected():
    with pytest.raises(HTTPException) as e:
        verify_signature(b'{"x":1}', sign(b"{}"), SECRET)
    assert e.value.status_code == 401


def test_missing_signature_rejected():
    with pytest.raises(HTTPException):
        verify_signature(b"{}", None, SECRET)


def test_adf_flattened():
    doc = {"type": "doc", "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "NameNode "},
                                          {"type": "text", "text": "crashed"}]},
        {"type": "codeBlock", "content": [{"type": "text", "text": "java.io.IOException"}]}]}
    assert adf_text(doc) == "NameNode crashed\njava.io.IOException"


def test_other_events_ignored(monkeypatch):
    monkeypatch.setattr(service.settings, "jira_webhook_secret", SECRET)
    body = json.dumps({"webhookEvent": "jira:issue_updated"}).encode()
    r = TestClient(service.app).post("/webhooks/jira", content=body,
                                     headers={"X-Hub-Signature": sign(body)})
    assert r.status_code == 200 and r.json()["status"] == "ignored"

def _post(event: dict):
    body = json.dumps(event).encode()
    return TestClient(service.app).post("/webhooks/jira", content=body,
                                        headers={"X-Hub-Signature": sign(body)})


def test_created_enqueues_triage_once(db, monkeypatch):
    monkeypatch.setattr(service.settings, "jira_webhook_secret", SECRET)
    event = {"webhookEvent": "jira:issue_created", "issue": {"key": "HDFS-1", "fields": {
        "summary": "dn fails", "created": "2026-10-02T12:00:00.000-0700",
        "description": "key AKIA1234567890ABCDEF leaked"}}}
    first, second = _post(event), _post(event)
    assert (first.status_code, second.status_code) == (202, 200)
    rows = db.execute("select type, payload::text from jobs").fetchall()
    assert len(rows) == 1 and rows[0][0] == "triage_ticket"
    assert "AKIA" not in rows[0][1]


def test_resolved_enqueues_index(db, monkeypatch):
    monkeypatch.setattr(service.settings, "jira_webhook_secret", SECRET)
    r = _post({"webhookEvent": "jira:issue_updated", "issue": {"key": "HDFS-2"},
               "changelog": {"items": [{"field": "resolution", "to": "1", "toString": "Fixed"}]}})
    assert r.status_code == 202
    assert db.execute("select type, payload->>'key' from jobs").fetchall() == [("index_ticket", "HDFS-2")]
