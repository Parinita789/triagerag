"""Tests marked `db` run against a separate triagerag_test database on the compose Postgres.
They skip when Postgres isn't reachable."""
from pathlib import Path

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from triagerag.config import settings

SCHEMA = Path(__file__).resolve().parents[1] / "scripts" / "schema.sql"
TEST_DB = "triagerag_test"
TABLES = "job_events, jobs, triages, chunks, ticket_links, tickets"


@pytest.fixture(scope="session")
def test_db_url() -> str:
    admin = make_conninfo(settings.database_url, dbname="postgres")
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=2) as conn:
            if not conn.execute("select 1 from pg_database where datname = %s", (TEST_DB,)).fetchone():
                conn.execute(f"create database {TEST_DB}")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable: {e}")
    url = make_conninfo(settings.database_url, dbname=TEST_DB)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
    return url


@pytest.fixture
def db(test_db_url: str, monkeypatch: pytest.MonkeyPatch):
    """A clean autocommit connection; code that opens its own connections uses the test DB too."""
    monkeypatch.setattr(settings, "database_url", test_db_url)
    with psycopg.connect(test_db_url, autocommit=True) as conn:
        conn.execute(f"truncate {TABLES} restart identity cascade")
        conn.execute("update index_state set bm25_version = 0")
        yield conn


def make_issue(key: str, summary: str, description: str, comments: list[str] = ()) -> dict:
    """A minimal Jira REST issue, shaped like the ones fetch_jira saves."""
    return {"key": key, "fields": {
        "summary": summary, "description": description,
        "status": {"name": "Resolved"}, "resolution": {"name": "Fixed"},
        "issuetype": {"name": "Bug"}, "components": [{"name": "datanode"}],
        "created": "2010-06-01T10:00:00.000+0000", "resolutiondate": "2010-07-01T10:00:00.000+0000",
        "issuelinks": [],
        "comment": {"comments": [
            {"author": {"name": "dev"}, "created": "2010-06-02T10:00:00.000+0000", "body": body}
            for body in comments]},
    }}


class StubLLM:
    """Answers every chat with a submit_diagnosis citing the first ticket key it was shown."""

    def __init__(self) -> None:
        self.calls = 0
        self.last_cache_hit = False

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        import json
        import re
        self.calls += 1
        keys = re.findall(r"\bHDFS-\d+\b", messages[-1]["content"])
        args = {"summary": "Block reports fail after restart.", "likely_cause": "Stale storage IDs.",
                "related_tickets": [{"key": k, "why": "same symptom"} for k in keys[:1]],
                "confidence": "medium", "abstain": not keys,
                "abstain_reason": "" if keys else "nothing related"}
        return {"choices": [{"finish_reason": "tool_calls", "message": {
                    "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {
                        "name": "submit_diagnosis", "arguments": json.dumps(args)}}]}}],
                "usage": {"total_tokens": 123}}
