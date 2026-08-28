"""Phase 4 — the append-only audit log.

Every policy decision and every money action lands here, with enough structure
that a human can reconstruct what happened and why WITHOUT reading the code.

Two properties make that claim real rather than aspirational:

1. APPEND-ONLY IS ENFORCED BY THE DATABASE, NOT BY DISCIPLINE. Triggers reject
   UPDATE and DELETE on the table outright, so tampering fails even from a raw
   `sqlite3` shell. A rule the code merely follows is a rule a future refactor
   can quietly break; a trigger is not.

2. EVENTS ARE NAMED, NOT PROSE. A fixed vocabulary (`EventType`) means a
   reviewer greps for `POLICY_DENIED` instead of parsing sentences, and a test
   can assert that every code path emits exactly one corresponding entry.

The `actor` column answers a question the event type alone cannot: WHO decided.
A human confirming a purchase and the policy engine approving one are different
kinds of event, and conflating them would hide the single most important fact
about this system -- that the LLM and the human can propose, but only the policy
engine authorises.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class EventType(str, Enum):
    """The fixed vocabulary. Add to it deliberately, never ad hoc."""

    # Intent (Phase 5 will emit the LLM-specific ones)
    PURCHASE_REQUESTED = "PURCHASE_REQUESTED"
    INTENT_PARSED = "INTENT_PARSED"
    PRICE_VALIDATED = "PRICE_VALIDATED"
    USER_CONFIRMED = "USER_CONFIRMED"
    USER_DECLINED = "USER_DECLINED"

    # Policy (Phase 3)
    POLICY_APPROVED = "POLICY_APPROVED"
    POLICY_DENIED = "POLICY_DENIED"

    # Idempotency (Phase 1)
    IDEMPOTENCY_EXECUTED = "IDEMPOTENCY_EXECUTED"
    IDEMPOTENCY_REPLAYED = "IDEMPOTENCY_REPLAYED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    IDEMPOTENCY_IN_PROGRESS = "IDEMPOTENCY_IN_PROGRESS"
    IDEMPOTENCY_RECLAIMED = "IDEMPOTENCY_RECLAIMED"

    # Money (Phase 2)
    PAYMENT_ATTEMPTED = "PAYMENT_ATTEMPTED"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_PENDING_VERIFICATION = "PAYMENT_PENDING_VERIFICATION"

    # Reconciliation (Phase 7)
    DIVERGENCE_DETECTED = "DIVERGENCE_DETECTED"
    DIVERGENCE_RESOLVED = "DIVERGENCE_RESOLVED"


class Actor(str, Enum):
    """Who caused the entry. Load-bearing for the trust story."""

    AGENT = "AGENT"                  # untrusted: proposes only
    HUMAN = "HUMAN"                  # confirms; confirmation is not authorisation
    POLICY_ENGINE = "POLICY_ENGINE"  # the only actor that authorises
    SYSTEM = "SYSTEM"                # idempotency layer, reconciliation
    PROVIDER = "PROVIDER"            # Razorpay


class AuditWriteError(RuntimeError):
    """The log could not be written.

    Deliberately fatal to the caller: this project logs BEFORE it executes, so
    an unwritable log blocks the money action rather than letting a payment
    happen with no record of it.
    """


@dataclass(frozen=True)
class AuditEntry:
    event_id: int
    event_type: EventType
    actor: Actor
    occurred_at: float
    request_id: Optional[str] = None
    agent_id: Optional[str] = None
    mandate_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    rule: Optional[str] = None
    reason: Optional[str] = None
    details: dict = field(default_factory=dict)

    def describe(self) -> str:
        """One readable line -- the unit a reviewer actually reads."""
        stamp = time.strftime("%H:%M:%S", time.localtime(self.occurred_at))
        parts = [f"[{stamp}] {self.event_type.value:<28} by {self.actor.value}"]
        if self.rule:
            parts.append(f"rule={self.rule}")
        if self.reason:
            parts.append(f"({self.reason})")
        return " ".join(parts)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    occurred_at     REAL NOT NULL,
    request_id      TEXT,
    agent_id        TEXT,
    mandate_id      TEXT,
    idempotency_key TEXT,
    rule            TEXT,
    reason          TEXT,
    details         TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);

-- Append-only, enforced by the database. These are the teeth behind the
-- invariant in CLAUDE.md: a past entry cannot be altered or removed, not even
-- from a raw sqlite3 shell.
CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE is not permitted');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only: DELETE is not permitted');
END;
"""


class AuditLog:
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

    @staticmethod
    def new_request_id() -> str:
        return f"req_{uuid.uuid4().hex[:16]}"

    def record(
        self,
        event_type: EventType,
        actor: Actor,
        *,
        request_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        mandate_id: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        rule: Optional[str] = None,
        reason: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> AuditEntry:
        """Append one entry. Raises AuditWriteError if it cannot be written."""
        occurred_at = self._clock()
        payload = json.dumps(details or {}, sort_keys=True, default=str)
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "INSERT INTO audit_log (event_type, actor, occurred_at, "
                    "request_id, agent_id, mandate_id, idempotency_key, rule, "
                    "reason, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_type.value,
                        actor.value,
                        occurred_at,
                        request_id,
                        agent_id,
                        mandate_id,
                        idempotency_key,
                        rule,
                        reason,
                        payload,
                    ),
                )
                event_id = cur.lastrowid
                conn.execute("COMMIT")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise AuditWriteError(
                f"could not append {event_type.value} to the audit log: {exc}"
            ) from exc

        return AuditEntry(
            event_id=event_id,
            event_type=event_type,
            actor=actor,
            occurred_at=occurred_at,
            request_id=request_id,
            agent_id=agent_id,
            mandate_id=mandate_id,
            idempotency_key=idempotency_key,
            rule=rule,
            reason=reason,
            details=details or {},
        )

    # -- reading it back ---------------------------------------------------

    def all(self) -> list[AuditEntry]:
        return self._query("SELECT * FROM audit_log ORDER BY event_id")

    def for_request(self, request_id: str) -> list[AuditEntry]:
        """Every entry for one request, in order -- the story of what happened."""
        return self._query(
            "SELECT * FROM audit_log WHERE request_id = ? ORDER BY event_id",
            (request_id,),
        )

    def of_type(self, event_type: EventType) -> list[AuditEntry]:
        return self._query(
            "SELECT * FROM audit_log WHERE event_type = ? ORDER BY event_id",
            (event_type.value,),
        )

    def count_of(self, event_type: EventType, request_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = ?"
        params: tuple[Any, ...] = (event_type.value,)
        if request_id is not None:
            sql += " AND request_id = ?"
            params += (request_id,)
        conn = self._connect()
        try:
            return conn.execute(sql, params).fetchone()["n"]
        finally:
            conn.close()

    def timeline(self, request_id: str) -> str:
        """The log rendered for a human, with no access to the code."""
        entries = self.for_request(request_id)
        if not entries:
            return f"no audit entries for {request_id}"
        lines = [f"Request {request_id}"]
        lines += [f"  {e.describe()}" for e in entries]
        return "\n".join(lines)

    def _query(self, sql: str, params: tuple = ()) -> list[AuditEntry]:
        conn = self._connect()
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_entry(r) for r in rows]


def _row_to_entry(row: sqlite3.Row) -> AuditEntry:
    return AuditEntry(
        event_id=row["event_id"],
        event_type=EventType(row["event_type"]),
        actor=Actor(row["actor"]),
        occurred_at=row["occurred_at"],
        request_id=row["request_id"],
        agent_id=row["agent_id"],
        mandate_id=row["mandate_id"],
        idempotency_key=row["idempotency_key"],
        rule=row["rule"],
        reason=row["reason"],
        details=json.loads(row["details"]),
    )
