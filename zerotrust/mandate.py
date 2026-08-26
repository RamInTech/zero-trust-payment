"""Phase 3 — the mandate: a spending boundary agreed in advance.

A mandate is what makes "bounded" concrete. Before an agent may spend anything,
the merchant states four limits: how much per transaction, on what items, until
when, and how often. Everything the policy engine does is a comparison against
one of these fields.

Schema decisions (previously open item #1 in RAZORPAY.md):
  - Amounts are integer paise, never floats. Money in floating point is a
    rounding bug waiting to be discovered in production.
  - `allowed_skus` is an allowlist, not a denylist. A denylist fails open --
    anything the merchant forgot to name is permitted -- which is the wrong
    default for money.
  - Velocity is a SLIDING window ("N in the last W seconds"), not a fixed one.
    A fixed window lets an agent spend 2x the cap by straddling the boundary,
    which Phase 6's adversarial suite would legitimately exploit.
  - Mandates live in SQLite alongside the idempotency store, because velocity
    counting needs durable purchase history anyway.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mandates (
    mandate_id            TEXT PRIMARY KEY,
    agent_id              TEXT NOT NULL,
    max_amount_paise      INTEGER NOT NULL,
    allowed_skus          TEXT NOT NULL,      -- JSON array
    currency              TEXT NOT NULL DEFAULT 'INR',
    expires_at            REAL NOT NULL,
    velocity_limit        INTEGER NOT NULL,
    velocity_window_secs  REAL NOT NULL,
    created_at            REAL NOT NULL,
    revoked_at            REAL
);
CREATE INDEX IF NOT EXISTS idx_mandates_agent ON mandates(agent_id);
"""


@dataclass(frozen=True)
class Mandate:
    agent_id: str
    max_amount_paise: int
    allowed_skus: frozenset[str]
    expires_at: float
    velocity_limit: int
    velocity_window_secs: float = 3600.0
    currency: str = "INR"
    mandate_id: str = field(default_factory=lambda: f"mdt_{uuid.uuid4().hex[:12]}")
    created_at: float = field(default_factory=time.time)
    revoked_at: Optional[float] = None

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def allows_sku(self, sku: str) -> bool:
        return sku in self.allowed_skus


class MandateStore:
    def __init__(self, db_path: str, clock: Callable[[], float] = time.time) -> None:
        self.db_path = db_path
        self._clock = clock
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def issue(self, mandate: Mandate) -> Mandate:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO mandates (mandate_id, agent_id, max_amount_paise, "
                "allowed_skus, currency, expires_at, velocity_limit, "
                "velocity_window_secs, created_at, revoked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    mandate.mandate_id,
                    mandate.agent_id,
                    mandate.max_amount_paise,
                    json.dumps(sorted(mandate.allowed_skus)),
                    mandate.currency,
                    mandate.expires_at,
                    mandate.velocity_limit,
                    mandate.velocity_window_secs,
                    mandate.created_at,
                    mandate.revoked_at,
                ),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()
        return mandate

    def revoke(self, mandate_id: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE mandates SET revoked_at = ? WHERE mandate_id = ?",
                (self._clock(), mandate_id),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def get(self, mandate_id: str) -> Optional[Mandate]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM mandates WHERE mandate_id = ?", (mandate_id,)
            ).fetchone()
        finally:
            conn.close()
        return _row_to_mandate(row) if row else None

    def active_for_agent(self, agent_id: str) -> Optional[Mandate]:
        """The newest non-revoked mandate for this agent.

        Expiry is deliberately NOT filtered here. An expired mandate must reach
        the policy engine so the denial can say "expired" rather than the
        engine reporting the agent has no mandate at all -- a specific reason
        beats a generic one (see CLAUDE.md invariants).
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM mandates WHERE agent_id = ? AND revoked_at IS NULL "
                "ORDER BY created_at DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_mandate(row) if row else None


def _row_to_mandate(row: sqlite3.Row) -> Mandate:
    return Mandate(
        mandate_id=row["mandate_id"],
        agent_id=row["agent_id"],
        max_amount_paise=row["max_amount_paise"],
        allowed_skus=frozenset(json.loads(row["allowed_skus"])),
        currency=row["currency"],
        expires_at=row["expires_at"],
        velocity_limit=row["velocity_limit"],
        velocity_window_secs=row["velocity_window_secs"],
        created_at=row["created_at"],
        revoked_at=row["revoked_at"],
    )
