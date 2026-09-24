"""Phase 1 — Idempotency Core.

The guarantee: a money action carrying a given idempotency key executes at most
once, no matter how many times it is retried, replayed, or raced.

The mechanism is a single unique constraint. Claiming a key is an INSERT against
a PRIMARY KEY column; exactly one caller's INSERT can succeed. That is what
makes the guarantee hold under genuinely concurrent callers -- the database
serialises the claim, so there is no application-level lock for a race to slip
past. Keep it this simple.

Phase 10 (Postgres): the claim is `INSERT ... ON CONFLICT DO NOTHING`. When two
callers race on one key, the second INSERT waits for the first to commit and
then does nothing -- the unique constraint still decides. Whoever lost then
reads the winner's row `FOR UPDATE`, which serialises the follow-up decisions
(replay, conflict, stale reclaim) on that one key only, where SQLite's
`BEGIN IMMEDIATE` used to serialise every writer in the file.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

from zerotrust.db import Database

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
    #: A stale claimant turned out to have charged before it died. Its key is
    #: completed from the provider's own record of that charge, and the action
    #: is NOT run again. Before Phase 12, this case re-ran the action -- and
    #: charged the customer a second time.
    RECOVERED = "RECOVERED"


#: Outcomes for which the underlying action was actually invoked.
EXECUTING_OUTCOMES = frozenset({Outcome.EXECUTED, Outcome.RECLAIMED})


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    key: str
    response: Optional[dict] = None
    reason: Optional[str] = None
    attempts: int = 1
    #: When this caller's claim was taken. A later write checks it is still
    #: the value on the row, so a claim lost to another caller cannot act.
    claimed_at: Optional[float] = None
    #: For a reclaim: when the stalled claimant took the key. How long ago
    #: that was decides whether "the provider has no order" means anything yet.
    prior_claimed_at: Optional[float] = None

    @property
    def executed(self) -> bool:
        return self.outcome in EXECUTING_OUTCOMES


class StaleCheck(str, Enum):
    """What the provider says about a stalled claimant's attempt."""

    EXECUTED = "EXECUTED"          # it charged; complete from that order
    NOT_EXECUTED = "NOT_EXECUTED"  # it did not; safe to run the action
    UNKNOWN = "UNKNOWN"            # cannot tell; freeze, do not guess


@dataclass(frozen=True)
class StaleVerdict:
    check: StaleCheck
    response: Optional[dict] = None
    reason: Optional[str] = None

    @classmethod
    def executed(cls, response: dict) -> "StaleVerdict":
        return cls(StaleCheck.EXECUTED, response=response)

    @classmethod
    def not_executed(cls) -> "StaleVerdict":
        return cls(StaleCheck.NOT_EXECUTED)

    @classmethod
    def unknown(cls, reason: str) -> "StaleVerdict":
        return cls(StaleCheck.UNKNOWN, reason=reason)


#: Asked, outside any lock, before a reclaimed key's action runs. The store
#: knows nothing about providers; the caller that does supplies this.
VerifyStale = Callable[[Result], StaleVerdict]


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
    claimed_at   DOUBLE PRECISION NOT NULL,
    completed_at DOUBLE PRECISION,
    attempts     INTEGER NOT NULL DEFAULT 1,
    response     TEXT,
    created_at   DOUBLE PRECISION NOT NULL
);
"""


#: Work that must commit in the same transaction as a status change. Receives
#: that transaction's connection and the record's attempt number.
Also = Callable[[Any, int], None]


class CompletionNotRecorded(RuntimeError):
    """The action ran, but its completion and posting could not be written.

    `attempts` is the frozen record's attempt number, or None if even the
    freeze failed -- in which case the record is still PROCESSING.
    """

    def __init__(self, cause: BaseException, response: Any,
                 attempts: Optional[int]) -> None:
        super().__init__(f"completion not recorded: {cause}")
        self.cause = cause
        self.response = response
        self.attempts = attempts


class IdempotencyStore:
    """Wraps a callable so that it runs at most once per (key, payload)."""

    def __init__(
        self,
        db: Database,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db = db
        self.stale_after_seconds = stale_after_seconds
        self._clock = clock
        db.apply_schema(_SCHEMA)

    def get(self, key: str, agent_id: Optional[str] = None) -> Optional[dict]:
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT * FROM idempotency_records WHERE key = %s",
                (scope_key(key, agent_id),),
            ).fetchone()

    # -- the claim --------------------------------------------------------

    def _claim(self, key: str, fp: str) -> Result:
        """Decide, atomically, whether this caller may run the action."""
        now = self._clock()
        with self.db.transaction() as conn:
            inserted = conn.execute(
                "INSERT INTO idempotency_records "
                "(key, fingerprint, status, claimed_at, attempts, created_at) "
                "VALUES (%s, %s, %s, %s, 1, %s) "
                "ON CONFLICT (key) DO NOTHING RETURNING key",
                (key, fp, PROCESSING, now, now),
            ).fetchone()
            if inserted is not None:
                return Result(Outcome.EXECUTED, key)

            # Someone already holds this key. Lock their row, then work out
            # what that means -- nobody else can change it while we decide.
            row = conn.execute(
                "SELECT * FROM idempotency_records WHERE key = %s FOR UPDATE",
                (key,),
            ).fetchone()

            if row["fingerprint"] != fp:
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
                return Result(
                    Outcome.REPLAYED,
                    key,
                    response=json.loads(row["response"]) if row["response"] else None,
                    attempts=row["attempts"],
                )

            if row["status"] == PENDING_VERIFICATION:
                # The dangerous case. A previous attempt may or may not have
                # moved money. Staleness must NOT rescue this record:
                # reclaiming it is precisely how an unknown outcome turns
                # into a second charge. Only reconciliation resolves it.
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
                    "SET status = %s, claimed_at = %s, attempts = attempts + 1 "
                    "WHERE key = %s",
                    (PROCESSING, now, key),
                )
                return Result(Outcome.EXECUTED, key, attempts=row["attempts"] + 1)

            # status == PROCESSING
            age = now - row["claimed_at"]
            if age >= self.stale_after_seconds:
                # The claimant almost certainly died. Reclaim -- the row lock
                # already makes this caller the only one deciding, and the
                # claimed_at guard keeps the update honest regardless.
                cur = conn.execute(
                    "UPDATE idempotency_records "
                    "SET claimed_at = %s, attempts = attempts + 1 "
                    "WHERE key = %s AND claimed_at = %s AND status = %s",
                    (now, key, row["claimed_at"], PROCESSING),
                )
                if cur.rowcount == 1:
                    return Result(
                        Outcome.RECLAIMED, key, attempts=row["attempts"] + 1,
                        claimed_at=now, prior_claimed_at=row["claimed_at"],
                    )
                return Result(
                    Outcome.IN_PROGRESS,
                    key,
                    reason="another caller reclaimed this key first",
                    attempts=row["attempts"],
                )

            return Result(
                Outcome.IN_PROGRESS,
                key,
                reason=(
                    f"key claimed {age:.3f}s ago and still in flight; "
                    f"retry after {self.stale_after_seconds - age:.3f}s"
                ),
                attempts=row["attempts"],
            )

    def _finish(self, key: str, response: dict, also: Optional[Also] = None) -> None:
        """Mark a key COMPLETED -- and run `also` in the same transaction."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE idempotency_records "
                "SET status = %s, response = %s, completed_at = %s WHERE key = %s "
                "RETURNING attempts",
                (COMPLETED, json.dumps(response), self._clock(), key),
            ).fetchone()
            if also is not None and row is not None:
                also(conn, row["attempts"])

    def _abandon(self, key: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE idempotency_records SET status = %s WHERE key = %s",
                (FAILED, key),
            )

    def mark_pending_verification(
        self, key: str, reason: str, agent_id: Optional[str] = None,
        *, also: Optional[Also] = None,
    ) -> int:
        """Freeze a key whose outcome is unknown; see `_freeze`.

        `also` runs in the same transaction (the gateway posts suspense there).
        If it fails, the key is frozen on its own and the failure is raised:
        a record left PROCESSING could be reclaimed and charged again, so the
        freeze matters more than the posting that accompanies it. The missing
        posting stays visible in `Ledger.discrepancies()`.
        """
        if also is None:
            return self._freeze(key, reason, agent_id)
        try:
            return self._freeze(key, reason, agent_id, also)
        except KeyError:
            raise
        except Exception:
            self._freeze(key, reason, agent_id)
            raise

    def _freeze(
        self, key: str, reason: str, agent_id: Optional[str] = None,
        also: Optional[Also] = None,
    ) -> int:
        """Record that this key's true outcome is unknown.

        Called when the provider call timed out, or when the process died
        between the provider succeeding and the completion write. The record
        is frozen here until reconciliation resolves it.

        Returns the record's `attempts`, read in the same statement that froze
        it. A pending record cannot be reclaimed, so the number is stable --
        the ledger keys suspense postings on it, and reading it separately
        afterwards could see a later attempt's value.
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE idempotency_records SET status = %s, response = %s "
                "WHERE key = %s RETURNING attempts",
                (
                    PENDING_VERIFICATION,
                    json.dumps({"pending_reason": reason}),
                    scope_key(key, agent_id),
                ),
            ).fetchone()

            if row is None:
                # Marking a key that was never claimed means a caller believes
                # a money action is in doubt for a request this store has
                # never seen. Failing loudly beats a silent no-op that would
                # leave the doubt unrecorded -- which is the one thing this
                # state exists to prevent.
                raise KeyError(
                    f"cannot mark '{key}' pending verification: no such "
                    f"idempotency record"
                )
            if also is not None:
                also(conn, row["attempts"])
        return row["attempts"]

    def resolve_verified(
        self, key: str, response: dict, agent_id: Optional[str] = None,
        *, also: Optional[Also] = None,
    ) -> None:
        """Reconciliation confirmed the action DID happen. Record the truth,
        and run `also` (the reconciler books the sale) in the same transaction."""
        self._finish(scope_key(key, agent_id), response, also)

    def resolve_not_executed(
        self, key: str, agent_id: Optional[str] = None,
        *, also: Optional[Also] = None,
    ) -> None:
        """Reconciliation confirmed the action did NOT happen; retry is safe.
        `also` (reversing suspense) commits with the status change or not at all."""
        with self.db.transaction() as conn:
            row = conn.execute(
                "UPDATE idempotency_records SET status = %s, response = NULL "
                "WHERE key = %s RETURNING attempts",
                (FAILED, scope_key(key, agent_id)),
            ).fetchone()
            if also is not None and row is not None:
                also(conn, row["attempts"])

    def records(self) -> list[dict]:
        """Every record's key, status, attempt count and creation time.

        Read by the ledger's discrepancy check. Deliberately excludes the
        stored response: the check needs to know what state a key is in, not
        what the provider said.
        """
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT key, status, attempts, created_at "
                "FROM idempotency_records ORDER BY claimed_at"
            ).fetchall()

    def pending_verification(self) -> list[dict]:
        """Every record whose outcome is still unknown."""
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT * FROM idempotency_records WHERE status = %s "
                "ORDER BY claimed_at",
                (PENDING_VERIFICATION,),
            ).fetchall()

    # -- public API -------------------------------------------------------

    def execute(
        self,
        key: str,
        payload: dict,
        action: Callable[[], Any],
        agent_id: Optional[str] = None,
        *,
        also: Optional[Also] = None,
        verify_stale: Optional[VerifyStale] = None,
        also_on_freeze: Optional[Also] = None,
    ) -> Result:
        """Run `action` at most once for this key.

        `verify_stale(claim)` is asked before a RECLAIMED key's action runs.
        A stale claim means the previous claimant stopped answering, not that
        it failed -- it may have charged and died before recording it. So:
        EXECUTED completes the key from the provider's order without running
        the action (outcome RECOVERED); UNKNOWN freezes the key for
        reconciliation, running `also_on_freeze` in the same transaction
        (outcome AWAITING_VERIFICATION); NOT_EXECUTED runs the action, but only
        after confirming this caller still holds the claim. Without a
        verifier, a reclaim runs the action unconditionally, as before
        Phase 12 -- which is safe only if the stalled claimant never charged.

        `also(conn, attempts)` runs inside the transaction that marks the key
        COMPLETED -- the gateway books the sale there -- so the record and the
        posting are written together or not at all. If that transaction fails
        after the action succeeded, the money has moved but cannot be recorded:
        the key is frozen PENDING_VERIFICATION for reconciliation, and
        `CompletionNotRecorded` is raised.

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

        if claim.outcome is Outcome.RECLAIMED and verify_stale is not None:
            settled = self._settle_stale_claim(scoped, key, claim, verify_stale,
                                               also, also_on_freeze)
            if settled is not None:
                return settled

        try:
            response = action()
        except Exception:
            self._abandon(scoped)
            raise

        self._complete(scoped, response, also)
        return Result(claim.outcome, key, response=response, attempts=claim.attempts)

    def _complete(self, scoped: str, response: Any, also: Optional[Also]) -> None:
        """Mark COMPLETED with `also`, freezing the key if that write fails."""
        try:
            self._finish(scoped, response, also)
        except Exception as exc:
            if also is None:
                raise
            # Rolled back as one unit, so nothing half-written exists. But the
            # action DID run: leaving the key PROCESSING would let a stale
            # reclaim run it again. Freeze it; reconciliation books the truth.
            try:
                attempts = self._freeze(
                    scoped, f"the action succeeded but its completion could "
                            f"not be recorded: {exc}")
            except Exception:  # noqa: BLE001 -- the database itself is gone
                attempts = None
            raise CompletionNotRecorded(exc, response, attempts) from exc

    # -- a reclaim that asks first (Phase 12) -----------------------------

    def _settle_stale_claim(
        self, scoped: str, key: str, claim: Result, verify_stale: VerifyStale,
        also: Optional[Also], also_on_freeze: Optional[Also],
    ) -> Optional[Result]:
        """Decide a reclaimed key from what the provider says, not from hope.

        Returns a finished Result, or None to mean "verified not executed, and
        this caller still holds the claim: run the action".

        The provider is asked with no row lock held. This caller holds the key
        by its fresh `claimed_at` instead -- anyone else now sees IN_PROGRESS
        -- and every write below is fenced on that value, so if the check
        outlives the staleness window and someone else takes the key, this
        caller stands down rather than acting on a claim it no longer holds.
        """
        try:
            verdict = verify_stale(claim)
        except Exception as exc:  # noqa: BLE001 -- a failed check is not a "no"
            verdict = StaleVerdict.unknown(
                f"checking whether the stalled attempt charged failed: {exc}")

        if verdict.check is StaleCheck.EXECUTED:
            # The stalled claimant's charge is real. Record it; do not repeat it.
            self._complete(scoped, verdict.response, also)
            return Result(
                Outcome.RECOVERED, key, response=verdict.response,
                reason=("the stalled attempt had already charged; completed "
                        "from the provider's order without charging again"),
                attempts=claim.attempts)

        if verdict.check is StaleCheck.UNKNOWN:
            reason = verdict.reason or "the stalled attempt's outcome is unknown"
            attempts = self._freeze_held_claim(scoped, claim.claimed_at, reason,
                                               also_on_freeze)
            if attempts is None:
                return self._lost_claim(key, claim)
            return Result(Outcome.AWAITING_VERIFICATION, key, reason=reason,
                          attempts=attempts)

        # NOT_EXECUTED. Re-take the claim before acting: the check may have
        # outlived the staleness window, and a claim that has changed hands is
        # not this caller's to act on.
        if not self._renew_claim(scoped, claim.claimed_at):
            return self._lost_claim(key, claim)
        return None

    @staticmethod
    def _lost_claim(key: str, claim: Result) -> Result:
        return Result(
            Outcome.IN_PROGRESS, key,
            reason=("another caller took this key while the stalled attempt "
                    "was being verified"),
            attempts=claim.attempts)

    def _renew_claim(self, scoped: str, claimed_at: float) -> bool:
        with self.db.connection() as conn:
            cur = conn.execute(
                "UPDATE idempotency_records SET claimed_at = %s "
                "WHERE key = %s AND claimed_at = %s AND status = %s",
                (self._clock(), scoped, claimed_at, PROCESSING))
        return cur.rowcount == 1

    def _freeze_held_claim(self, scoped: str, claimed_at: float, reason: str,
                           also: Optional[Also]) -> Optional[int]:
        """Freeze the key if this caller still holds it. None if it does not.

        `also` (a suspense posting) commits with the freeze; if it fails, the
        key is frozen without it -- the freeze is what stops a second charge.
        """
        def freeze(with_also: Optional[Also]) -> Optional[int]:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "UPDATE idempotency_records SET status = %s, response = %s "
                    "WHERE key = %s AND claimed_at = %s AND status = %s "
                    "RETURNING attempts",
                    (PENDING_VERIFICATION, json.dumps({"pending_reason": reason}),
                     scoped, claimed_at, PROCESSING)).fetchone()
                if row is None:
                    return None
                if with_also is not None:
                    with_also(conn, row["attempts"])
                return row["attempts"]

        if also is None:
            return freeze(None)
        try:
            return freeze(also)
        except Exception:  # noqa: BLE001
            return freeze(None)
