"""Apply plain-SQL migrations from ./migrations in filename order.

Usage: python -m ingest.migrate

Each file runs in its own transaction and is recorded in schema_migrations,
so re-running is a no-op. DATABASE_URL comes from the environment (or .env).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from dotenv import load_dotenv

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def pending(conn: psycopg.Connection) -> list[Path]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename   text PRIMARY KEY,
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    applied = {r[0] for r in conn.execute("SELECT filename FROM schema_migrations")}
    return [p for p in sorted(MIGRATIONS_DIR.glob("*.sql")) if p.name not in applied]


def migrate(database_url: str) -> list[str]:
    done: list[str] = []
    with psycopg.connect(database_url) as conn:
        with conn.transaction():
            todo = pending(conn)
        for path in todo:
            with conn.transaction():
                conn.execute(path.read_text())
                conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,))
            done.append(path.name)
    return done


def main() -> int:
    load_dotenv()
    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 1
    applied = migrate(url)
    print("applied: " + ", ".join(applied) if applied else "up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
