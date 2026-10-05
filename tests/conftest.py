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
