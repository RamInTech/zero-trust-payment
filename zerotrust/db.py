"""Phase 10 — the one place that knows how to reach Postgres.

Every store used to open its own SQLite file. They now share a `Database`: a
connection pool pointed at one Postgres schema. Three rules live here so no
store has to remember them.

EVERY CONNECTION IS AUTOCOMMIT, AND TRANSACTIONS ARE EXPLICIT. A psycopg
connection that is not in autocommit mode opens a transaction on its first
statement and keeps it open until told otherwise. A read left like that holds
its locks indefinitely -- found the hard way while probing Postgres for this
phase, when a forgotten `SELECT` blocked a `DROP SCHEMA` forever. So a store
either runs one statement (`connection()`), or wraps several in `transaction()`,
which commits on success and rolls back on any exception.

LOCKS REPLACE SQLITE'S WRITER LOCK. SQLite serialised every writer to a file,
and `BEGIN IMMEDIATE` leaned on that. Postgres lets writers run concurrently,
which is faster and means each guarantee has to say what it serialises on.
`lock()` takes a transaction-scoped advisory lock on a named key -- one agent's
velocity budget, one idempotency key's books, the tail of the audit chain --
released automatically at COMMIT or ROLLBACK.

ONE SCHEMA PER DATABASE OBJECT. The app uses a fixed schema; every test gets a
fresh, uniquely named one that is dropped afterwards, which gives the same
isolation `tmp_path` gave SQLite without creating a database per test.
"""

from __future__ import annotations

import os
import re
import uuid
from contextlib import contextmanager
from typing import Iterator

import psycopg
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_DSN = "dbname=zerotrust"
DEFAULT_SCHEMA = "zerotrust"

#: Schema names are interpolated into DDL (identifiers cannot be bound as
#: parameters), so they are restricted to a shape that cannot inject anything.
_SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class Database:
    """A connection pool bound to one schema."""

    def __init__(self, dsn: str = DEFAULT_DSN, schema: str = DEFAULT_SCHEMA, *,
                 max_connections: int = 20, owns_schema: bool = False) -> None:
        if not _SCHEMA_NAME.match(schema):
            raise ValueError(f"unsafe schema name: {schema!r}")
        self.dsn = dsn
        self.schema = schema
        self._owns_schema = owns_schema
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        # application_name tags every backend this pool opens, so a tool can
        # find them in pg_stat_activity -- which is how Phase 11's chaos
        # harness terminates connections mid-transaction without touching
        # anyone else's.
        self._conninfo = make_conninfo(
            dsn, options=f"-c search_path={schema}",
            application_name=f"zerotrust-{schema}")
        self._pool = ConnectionPool(
            self._conninfo, min_size=1, max_size=max_connections,
            kwargs={"autocommit": True, "row_factory": dict_row},
            name=f"zerotrust-{schema}", open=True)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_env(cls, var: str = "DATABASE_URL",
                 schema: str = DEFAULT_SCHEMA) -> "Database":
        return cls(os.environ.get(var, DEFAULT_DSN), schema)

    @classmethod
    def fresh(cls, schema: str, dsn: str | None = None) -> "Database":
        """A named schema emptied first, then kept -- for the manual demo
        scripts, which start clean but leave their tables behind to inspect."""
        if not _SCHEMA_NAME.match(schema):
            raise ValueError(f"unsafe schema name: {schema!r}")
        dsn = dsn or os.environ.get("DATABASE_URL", DEFAULT_DSN)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        return cls(dsn, schema)

    @classmethod
    def create_temporary(cls, dsn: str = DEFAULT_DSN,
                         prefix: str = "tmp") -> "Database":
        """A fresh, uniquely named schema that `drop()` removes again."""
        return cls(dsn, f"{prefix}_{uuid.uuid4().hex[:12]}", owns_schema=True)

    @classmethod
    @contextmanager
    def temporary(cls, dsn: str = DEFAULT_DSN,
                  prefix: str = "tmp") -> Iterator["Database"]:
        db = cls.create_temporary(dsn, prefix)
        try:
            yield db
        finally:
            db.drop()

    # -- use ---------------------------------------------------------------

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """One pooled connection. Each statement commits on its own."""
        with self._pool.connection() as conn:
            yield conn

    @contextmanager
    def transaction(self) -> Iterator[psycopg.Connection]:
        """One pooled connection inside a transaction: all or nothing."""
        with self._pool.connection() as conn:
            with conn.transaction():
                yield conn

    def lock(self, conn: psycopg.Connection, key: str) -> None:
        """Serialise on `key` until the surrounding transaction ends.

        Namespaced by schema, because advisory locks are database-wide: two
        test schemas using the same key must not block each other.
        """
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     (f"{self.schema}:{key}",))

    def apply_schema(self, ddl: str) -> None:
        """Run DDL once, even when several stores start at the same moment.

        Concurrent `CREATE OR REPLACE FUNCTION` on one name can fail with
        "tuple concurrently updated", so schema changes take a lock too.
        """
        with self.transaction() as conn:
            self.lock(conn, "ddl")
            conn.execute(ddl)

    def outside_connection(self) -> psycopg.Connection:
        """A connection that does NOT come from the pool.

        For code standing in for someone outside the application -- the
        tamper demonstrations and the tests that attack the database directly.
        The caller closes it.
        """
        return psycopg.connect(self._conninfo, autocommit=True,
                               row_factory=dict_row)

    # -- teardown ----------------------------------------------------------

    def close(self) -> None:
        self._pool.close()

    def drop(self) -> None:
        """Close the pool and, for a temporary schema, delete it."""
        self.close()
        if self._owns_schema:
            with psycopg.connect(self.dsn, autocommit=True) as conn:
                conn.execute(f'DROP SCHEMA IF EXISTS "{self.schema}" CASCADE')
