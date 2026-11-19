"""Schema checks against the local DB. Each test runs in a transaction that is
rolled back, so no rows are left behind. Skipped if DATABASE_URL is unset."""

import os

import psycopg
import pytest
from dotenv import load_dotenv

from ingest.migrate import migrate

load_dotenv()
DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set")

HASH = "a" * 64


@pytest.fixture(scope="module", autouse=True)
def _migrated():
    migrate(DATABASE_URL)


@pytest.fixture
def cur():
    with psycopg.connect(DATABASE_URL) as conn:
        with conn.cursor() as c:
            yield c
        conn.rollback()


def _company(cur, name="Acme", ats="greenhouse"):
    cur.execute("INSERT INTO companies (name, ats, config) VALUES (%s, %s, '{}') RETURNING id", (name, ats))
    return cur.fetchone()[0]


def _job(cur, company_id, source_job_id="1", content_hash=HASH):
    cur.execute(
        """INSERT INTO jobs (company_id, source_job_id, title, url, content_hash, raw_payload)
           VALUES (%s, %s, 'Engineer', 'https://example.com/1', %s, '{}') RETURNING id, status""",
        (company_id, source_job_id, content_hash),
    )
    return cur.fetchone()


def test_job_defaults(cur):
    _, status = _job(cur, _company(cur))
    assert status == "open"


def test_unique_per_company(cur):
    a, b = _company(cur, "A"), _company(cur, "B")
    _job(cur, a, "x")
    _job(cur, b, "x")  # same source id at another company is fine
    with pytest.raises(psycopg.errors.UniqueViolation):
        _job(cur, a, "x")


def test_rejects_unknown_ats(cur):
    with pytest.raises(psycopg.errors.CheckViolation):
        _company(cur, ats="lever")


def test_rejects_bad_hash(cur):
    with pytest.raises(psycopg.errors.CheckViolation):
        _job(cur, _company(cur), content_hash="not-a-hash")


def test_migrate_is_idempotent():
    assert migrate(DATABASE_URL) == []
