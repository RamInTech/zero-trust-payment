"""Shared pytest setup.

Two jobs.

1. Load `.env`. The LLM-backed tests gate on `GROQ_API_KEY` /
   `ANTHROPIC_API_KEY` read straight from `os.environ`, and nothing else on
   that path loads the file -- without this they skipped silently and
   permanently while the key sat right there.

2. Give every test its own Postgres schema (Phase 10). `db` is one fresh schema,
   dropped after the test; `db_factory` makes as many as a test needs, for the
   tests that want two genuinely separate stores. The suite needs a running
   Postgres, and says so immediately rather than failing 600 tests one at a
   time: a suite that cannot reach its database has proven nothing.
"""

from __future__ import annotations

import os

import psycopg
import pytest
from dotenv import load_dotenv

from zerotrust.db import Database

load_dotenv()

TEST_DSN = os.environ.get("TEST_DATABASE_URL", "dbname=zerotrust_test")


def pytest_sessionstart(session):
    try:
        psycopg.connect(TEST_DSN, connect_timeout=3).close()
    except psycopg.OperationalError as exc:
        pytest.exit(
            f"\nPostgres is not reachable at {TEST_DSN!r}.\n{exc}\n"
            "Start it (brew services start postgresql@17), create the test "
            "database (createdb zerotrust_test), or point TEST_DATABASE_URL "
            "at one.",
            returncode=2,
        )


@pytest.fixture
def db_factory():
    made: list[Database] = []

    def make(prefix: str = "t") -> Database:
        db = Database.create_temporary(TEST_DSN, prefix)
        made.append(db)
        return db

    yield make
    for db in made:
        db.drop()


@pytest.fixture
def db(db_factory) -> Database:
    return db_factory()


@pytest.fixture(scope="module")
def module_db():
    """One schema shared by a whole module, for expensive stateful runs."""
    db = Database.create_temporary(TEST_DSN, "m")
    yield db
    db.drop()
