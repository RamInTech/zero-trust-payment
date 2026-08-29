"""Phase 1 — Idempotency Core.

The guarantee: a money action carrying a given idempotency key executes at most
once, no matter how many times it is retried, replayed, or raced.

The mechanism is a single unique constraint. Claiming a key is an INSERT against
a PRIMARY KEY column; exactly one caller's INSERT can succeed and everyone else
gets an IntegrityError. That is what makes the guarantee hold under genuinely
concurrent callers -- the database serialises the claim, so there is no
application-level lock for a race to slip past. Keep it this simple.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

# Stored record states.
PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"
#: The provider call's true outcome is UNKNOWN -- a timeout, or a crash between
#: the provider succeeding and this record being written. Never reclaimed by
#: the staleness timeout, because retrying could double charge. Only
#: reconciliation (Phase 7) may move a record out of this state.
PENDING_VERIFICATION = "PENDING_VERIFICATION"

DEFAULT_STALE_AFTER_SECONDS = 30.0


class Outcome(str, Enum):
    """What the wrapper did with a request. One of these per call, always."""

    EXECUTED = "EXECUTED"          # key was new; the real action ran
    REPLAYED = "REPLAYED"          # key completed earlier; saved result returned
    RECLAIMED = "RECLAIMED"        # prior claimant went stale; we ran the action
    IN_PROGRESS = "IN_PROGRESS"    # key is genuinely mid-flight; caller retries later
    CONFLICT = "REJECTED_CONFLICT" # key reused with a different payload; rejected
    #: A previous attempt's outcome is unknown. Blocked until reconciliation
    #: resolves it -- retrying here is exactly how a timeout becomes a double
    #: charge, so this state deliberately refuses to proceed.
    AWAITING_VERIFICATION = "AWAITING_VERIFICATION"


#: Outcomes for which the underlying action was actually invoked.
EXECUTING_OUTCOMES = frozenset({Outcome.EXECUTED, Outcome.RECLAIMED})


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    key: str
    response: Optional[dict] = None
    reason: Optional[str] = None
    attempts: int = 1

    @property
    def executed(self) -> bool:
        return self.outcome in EXECUTING_OUTCOMES


def scope_key(key: str, agent_id: Optional[str] = None) -> str:
    """Namespace a key to an agent, so two agents cannot collide on one string."""
    return f"{agent_id}:{key}" if agent_id else key


def fingerprint(payload: dict) -> str:
    """Stable hash of a payload, so 'same key, different payload' is detectable."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_records (
    key          TEXT PRIMARY KEY,   -- the unique constraint IS the guarantee
    fingerprint  TEXT NOT NULL,
    status       TEXT NOT NULL,
    claimed_at   REAL NOT NULL,
    completed_at REAL,
    attempts     INTEGER NOT NULL DEFAULT 1,
    response     TEXT,
    created_at   REAL NOT NULL
);
"""


class IdempotencyStore:
    """Wraps a callable so that it runs at most once per (key, payload)."""

    def __init__(
        self,
        db_path: str,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = db_path
        self.stale_after_seconds = stale_after_seconds
        self._clock = clock
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
        finally:
            conn.close()

    # -- plumbing ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        # isolation_level=None: transactions are opened explicitly with
        # BEGIN IMMEDIATE so the write lock is taken up front, not upgraded
        # mid-transaction (which is where SQLite deadlocks come from).
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, key: str, agent_id: Optional[str] = None) -> Optional[sqlite3.Row]:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM idempotency_records WHERE key = ?",
                (scope_key(key, agent_id),),
            ).fetchone()
        finally:
            conn.close()

    # -- the claim --------------------------------------------------------

    def _claim(self, key: str, fp: str) -> Result:
        """Decide, atomically, whether this caller may run the action."""
        now = self._clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO idempotency_records "
                    "(key, fingerprint, status, claimed_at, attempts, created_at) "
                    "VALUES (?, ?, ?, ?, 1, ?)",
                    (key, fp, PROCESSING, now, now),
                )
                conn.execute("COMMIT")
                return Result(Outcome.EXECUTED, key)
            except sqlite3.IntegrityError:
                # Someone already holds this key. Work out what that means.
                row = conn.execute(
                    "SELECT * FROM idempotency_records WHERE key = ?", (key,)
                ).fetchone()

                if row["fingerprint"] != fp:
                    conn.execute("COMMIT")
                    return Result(
                        Outcome.CONFLICT,
                        key,
                        reason=(
                            "idempotency key reused with a different payload; "
                            "the original request is unaffected"
                        ),
                        attempts=row["attempts"],
                    )

                if row["status"] == COMPLETED:
                    conn.execute("COMMIT")
                    return Result(
                        Outcome.REPLAYED,
                        key,
                        response=json.loads(row["response"]) if row["response"] else None,
                        attempts=row["attempts"],
                    )

                if row["status"] == PENDING_VERIFICATION:
                    # The dangerous case. A previous attempt may or may not
                    # have moved money. Staleness must NOT rescue this record:
                    # reclaiming it is precisely how an unknown outcome turns
                    # into a second charge. Only reconciliation resolves it.
                    conn.execute("COMMIT")
                    return Result(
                        Outcome.AWAITING_VERIFICATION,
                        key,
                        reason=(
                            "a previous attempt's outcome is unknown and is "
                            "awaiting reconciliation; retrying could double "
                            "charge"
                        ),
                        attempts=row["attempts"],
                    )

                if row["status"] == FAILED:
                    # The prior attempt raised before completing; nothing was
                    # recorded as done, so a fresh attempt may take the key.
                    conn.execute(
                        "UPDATE idempotency_records "
                        "SET status = ?, claimed_at = ?, attempts = attempts + 1 "
                        "WHERE key = ?",
                        (PROCESSING, now, key),
                    )
                    conn.execute("COMMIT")
                    return Result(Outcome.EXECUTED, key, attempts=row["attempts"] + 1)

                # status == PROCESSING
                age = now - row["claimed_at"]
                if age >= self.stale_after_seconds:
                    # The claimant almost certainly died. Reclaim, but only if
                    # nobody else moved claimed_at in the meantime.
                    cur = conn.execute(
                        "UPDATE idempotency_records "
                        "SET claimed_at = ?, attempts = attempts + 1 "
                        "WHERE key = ? AND claimed_at = ? AND status = ?",
                        (now, key, row["claimed_at"], PROCESSING),
                    )
                    won = cur.rowcount == 1
                    conn.execute("COMMIT")
                    if won:
                        return Result(
                            Outcome.RECLAIMED, key, attempts=row["attempts"] + 1
                        )
                    return Result(
                        Outcome.IN_PROGRESS,
                        key,
                        reason="another caller reclaimed this key first",
                        attempts=row["attempts"],
                    )

                conn.execute("COMMIT")
                return Result(
                    Outcome.IN_PROGRESS,
                    key,
                    reason=(
                        f"key claimed {age:.3f}s ago and still in flight; "
                        f"retry after {self.stale_after_seconds - age:.3f}s"
                    ),
                    attempts=row["attempts"],
                )
        finally:
            conn.close()

    def _finish(self, key: str, response: dict) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_records "
                "SET status = ?, response = ?, completed_at = ? WHERE key = ?",
                (COMPLETED, json.dumps(response), self._clock(), key),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def _abandon(self, key: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_records SET status = ? WHERE key = ?",
                (FAILED, key),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def mark_pending_verification(
        self, key: str, reason: str, agent_id: Optional[str] = None
    ) -> None:
        """Record that this key's true outcome is unknown.

        Called when the provider call timed out, or when the process died
        between the provider succeeding and the completion write. The record
        is frozen here until reconciliation resolves it.
        """
        scoped = scope_key(key, agent_id)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE idempotency_records SET status = ?, response = ? "
                "WHERE key = ?",
                (
                    PENDING_VERIFICATION,
                    json.dumps({"pending_reason": reason}),
                    scoped,
                ),
            )
            updated = cur.rowcount
            conn.execute("COMMIT")
        finally:
            conn.close()

        if updated != 1:
            # Marking a key that was never claimed means a caller believes a
            # money action is in doubt for a request this store has never
            # seen. Failing loudly beats a silent no-op that would leave the
            # doubt unrecorded -- which is the one thing this state exists to
            # prevent.
            raise KeyError(
                f"cannot mark '{key}' pending verification: no such "
                f"idempotency record"
            )

    def resolve_verified(
        self, key: str, response: dict, agent_id: Optional[str] = None
    ) -> None:
        """Reconciliation confirmed the action DID happen. Record the truth."""
        self._finish(scope_key(key, agent_id), response)

    def resolve_not_executed(
        self, key: str, agent_id: Optional[str] = None
    ) -> None:
        """Reconciliation confirmed the action did NOT happen; retry is safe."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE idempotency_records SET status = ?, response = NULL "
                "WHERE key = ?",
                (FAILED, scope_key(key, agent_id)),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def pending_verification(self) -> list[sqlite3.Row]:
        """Every record whose outcome is still unknown."""
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT * FROM idempotency_records WHERE status = ? "
                "ORDER BY claimed_at",
                (PENDING_VERIFICATION,),
            ).fetchall()
        finally:
            conn.close()

    # -- public API -------------------------------------------------------

    def execute(
        self,
        key: str,
        payload: dict,
        action: Callable[[], Any],
        agent_id: Optional[str] = None,
    ) -> Result:
        """Run `action` at most once for this key.

        `action` is invoked only for EXECUTED and RECLAIMED outcomes. For
        REPLAYED the saved response comes back instead; for IN_PROGRESS and
        CONFLICT nothing runs at all.

        `agent_id` namespaces the key. Without it, two unrelated agents that
        happen to pick the same key string would collide -- one would see the
        other's result replayed back, or be blocked by it. Callers that handle
        more than one agent should always pass it; it is optional only so that
        single-agent callers (and Phase 1's tests) keep working unchanged.
        """
        scoped = scope_key(key, agent_id)
        fp = fingerprint(payload)
        claim = self._claim(scoped, fp)
        if not claim.executed:
            return Result(
                claim.outcome,
                key,
                response=claim.response,
                reason=claim.reason,
                attempts=claim.attempts,
            )

        try:
            response = action()
        except Exception:
            self._abandon(scoped)
            raise

        self._finish(scoped, response)
        return Result(claim.outcome, key, response=response, attempts=claim.attempts)
