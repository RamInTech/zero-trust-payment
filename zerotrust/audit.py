"""Phase 4 — the append-only audit log.

Every policy decision and every money action lands here, with enough structure
that a human can reconstruct what happened and why WITHOUT reading the code.

Three properties make that claim real rather than aspirational:

1. APPEND-ONLY IS ENFORCED BY THE DATABASE, NOT BY DISCIPLINE. Triggers reject
   UPDATE and DELETE on the table outright, so tampering fails even from a raw
   `psql` session. A rule the code merely follows is a rule a future refactor
   can quietly break; a trigger is not.

   Phase 10 (Postgres) adds a third trigger, and the reason is worth knowing:
   Postgres's TRUNCATE empties a table WITHOUT firing row-level triggers. The
   UPDATE and DELETE triggers alone would have let `TRUNCATE audit_log` wipe
   the whole history in one statement -- a hole SQLite never had, because
   SQLite has no TRUNCATE. A statement-level BEFORE TRUNCATE trigger closes it.

2. EVENTS ARE NAMED, NOT PROSE. A fixed vocabulary (`EventType`) means a
   reviewer greps for `POLICY_DENIED` instead of parsing sentences, and a test
   can assert that every code path emits exactly one corresponding entry.

3. ENTRIES ARE HASH-CHAINED. Each row carries the SHA-256 of its own contents
   linked to its predecessor's hash, so removing, reordering or editing a row
   breaks every link after it. The triggers above stop tampering THROUGH the
   database; the chain detects tampering that went AROUND it -- a restored
   backup, a row rewritten after someone dropped the triggers.

   Be precise about what this does and does not buy, because the difference
   matters. It makes PARTIAL tampering detectable. It does NOT make a complete
   rewrite detectable on its own: anyone who can drop the triggers can also
   recompute every subsequent hash, and the result verifies cleanly. What
   closes that hole is an anchor held somewhere the attacker cannot reach --
   `head()` returns exactly that value, and comparing it against an
   independently recorded copy is what turns "the chain is self-consistent"
   into "the chain is the one I saw yesterday". Storing the anchor is out of
   scope here and is named as such rather than implied.

The `actor` column answers a question the event type alone cannot: WHO decided.
A human confirming a purchase and the policy engine approving one are different
kinds of event, and conflating them would hide the single most important fact
about this system -- that the LLM and the human can propose, but only the policy
engine authorises.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import psycopg

from zerotrust.db import Database

#: The `prev_hash` of the first entry in a chain. A fixed, recognisable value
#: rather than NULL, so the genesis link is an ordinary, verifiable link.
GENESIS_HASH = "0" * 64


class EventType(str, Enum):
    """The fixed vocabulary. Add to it deliberately, never ad hoc."""

    # Intent (Phase 5 will emit the LLM-specific ones)
    PURCHASE_REQUESTED = "PURCHASE_REQUESTED"
    #: The merchant's recommender offered an add-on. Logged even when nobody
    #: takes it, because attach rate is meaningless without the denominator --
    #: and because an upsell nobody sees the offers for is unauditable.
    SUGGESTION_OFFERED = "SUGGESTION_OFFERED"
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
    #: Phase 12: a stale key whose stalled attempt had already charged,
    #: completed from the provider's order instead of charged again.
    IDEMPOTENCY_RECOVERED = "IDEMPOTENCY_RECOVERED"

    # Money (Phase 2)
    PAYMENT_ATTEMPTED = "PAYMENT_ATTEMPTED"
    PAYMENT_CAPTURED = "PAYMENT_CAPTURED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYMENT_PENDING_VERIFICATION = "PAYMENT_PENDING_VERIFICATION"

    # Reconciliation (Phase 7)
    DIVERGENCE_DETECTED = "DIVERGENCE_DETECTED"
    DIVERGENCE_RESOLVED = "DIVERGENCE_RESOLVED"

    # Inbound webhooks (Phase 7). A received webhook informs reconciliation and
    # never writes a payment outcome, so these sit apart from the money events
    # above rather than among them.
    WEBHOOK_RECEIVED = "WEBHOOK_RECEIVED"
    WEBHOOK_REJECTED = "WEBHOOK_REJECTED"


class Actor(str, Enum):
    """Who caused the entry. Load-bearing for the trust story."""

    AGENT = "AGENT"                  # untrusted: proposes only
    HUMAN = "HUMAN"                  # confirms; confirmation is not authorisation
    POLICY_ENGINE = "POLICY_ENGINE"  # the only actor that authorises
    SYSTEM = "SYSTEM"                # idempotency layer, reconciliation
    PROVIDER = "PROVIDER"            # Razorpay, cryptographically established
    #: An inbound caller whose identity was NOT established -- a webhook that
    #: failed signature verification. Distinct from PROVIDER on purpose:
    #: filing a forged delivery under PROVIDER would put a claim in the log
    #: that the log's own evidence contradicts.
    UNVERIFIED = "UNVERIFIED"


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


#: Names of the triggers that make the log append-only. Read back from the
#: live schema by the reference client, so a viewer sees the mechanism itself.
APPEND_ONLY_TRIGGERS = ("audit_log_no_update", "audit_log_no_delete",
                        "audit_log_no_truncate")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    event_id        BIGINT PRIMARY KEY,
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    occurred_at     DOUBLE PRECISION NOT NULL,
    request_id      TEXT,
    agent_id        TEXT,
    mandate_id      TEXT,
    idempotency_key TEXT,
    rule            TEXT,
    reason          TEXT,
    -- TEXT, deliberately not JSONB. The hash covers this exact string, and
    -- JSONB normalises key order and whitespace on the way in -- every entry
    -- would then fail verification without anyone having touched it.
    details         TEXT NOT NULL DEFAULT '{}',
    prev_hash       TEXT NOT NULL,
    entry_hash      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON audit_log(request_id);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);

-- Append-only, enforced by the database. These are the teeth behind the
-- invariant in CLAUDE.md: a past entry cannot be altered or removed, not even
-- from a raw psql session.
CREATE OR REPLACE FUNCTION audit_log_refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is not permitted', TG_OP
        USING ERRCODE = 'restrict_violation';
END
$$;

CREATE OR REPLACE TRIGGER audit_log_no_update
BEFORE UPDATE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_refuse_change();

CREATE OR REPLACE TRIGGER audit_log_no_delete
BEFORE DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION audit_log_refuse_change();

-- TRUNCATE skips row-level triggers entirely. Without this one, the two above
-- would stop a single-row edit and allow the whole history to be emptied.
CREATE OR REPLACE TRIGGER audit_log_no_truncate
BEFORE TRUNCATE ON audit_log
FOR EACH STATEMENT EXECUTE FUNCTION audit_log_refuse_change();
"""


def chain_hash(prev_hash: str, *, event_id: int, event_type: str, actor: str,
               occurred_at: float, request_id: Optional[str],
               agent_id: Optional[str], mandate_id: Optional[str],
               idempotency_key: Optional[str], rule: Optional[str],
               reason: Optional[str], details_json: str) -> str:
    """SHA-256 over the predecessor's hash and this entry's contents.

    Two details carry weight:

    `event_id` is inside the hash, so renumbering rows is detected even when
    the contents are untouched. `details_json` is hashed as the exact string
    that goes into the column -- re-serialising a parsed dict could differ by
    key order or float formatting and would break verification on a row nobody
    had touched, which is far worse than the tampering it looks for.

    The field separator is a NUL byte because it cannot occur in any of these
    values. A printable separator would let a crafted `reason` shift the field
    boundaries and forge a matching hash for different contents.
    """
    parts = [
        prev_hash, str(event_id), event_type, actor, repr(float(occurred_at)),
        request_id or "", agent_id or "", mandate_id or "",
        idempotency_key or "", rule or "", reason or "", details_json,
    ]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainReport:
    """The result of walking the chain. `intact` is the only headline."""

    intact: bool
    checked: int
    head: Optional[str]
    #: The first row whose link does not hold, and why. None when intact.
    broken_at: Optional[int] = None
    detail: Optional[str] = None

    @property
    def summary(self) -> str:
        """One honest line, for anywhere the boolean alone would mislead.

        `intact` is True on an empty log -- vacuously, as no link failed.
        Displaying that as "chain intact" would tell a viewer their entries
        are protected when there are none. The distinction between "nothing
        is broken" and "nothing is covered" belongs on screen.
        """
        if not self.intact:
            return f"BROKEN at entry {self.broken_at}"
        if self.checked == 0:
            return "no entries yet"
        return f"intact — {self.checked} verified"

    def as_dict(self) -> dict:
        return {
            "intact": self.intact,
            "summary": self.summary,
            "checked": self.checked,
            "head": self.head,
            "broken_at": self.broken_at,
            "detail": self.detail,
        }


class AuditLog:
    def __init__(self, db: Database, clock: Callable[[], float] = time.time) -> None:
        self.db = db
        self._clock = clock
        db.apply_schema(_SCHEMA)

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
            with self.db.transaction() as conn:
                # The tail is read under a lock held until COMMIT, which is
                # what makes the chain safe under concurrent writers. Reading
                # it without one would let two appenders see the same
                # predecessor and fork the chain into two branches sharing one
                # prev_hash -- and the fork would verify cleanly from either
                # side, so nothing downstream would ever notice.
                self.db.lock(conn, "audit_log:tail")
                tail = conn.execute(
                    "SELECT event_id, entry_hash FROM audit_log "
                    "ORDER BY event_id DESC LIMIT 1"
                ).fetchone()
                # The id is allocated here rather than by a sequence, because
                # it belongs inside the hash and the triggers make writing it
                # back in a second statement impossible.
                event_id = (tail["event_id"] + 1) if tail else 1
                prev_hash = tail["entry_hash"] if tail else GENESIS_HASH
                entry_hash = chain_hash(
                    prev_hash, event_id=event_id, event_type=event_type.value,
                    actor=actor.value, occurred_at=occurred_at,
                    request_id=request_id, agent_id=agent_id,
                    mandate_id=mandate_id, idempotency_key=idempotency_key,
                    rule=rule, reason=reason, details_json=payload,
                )
                conn.execute(
                    "INSERT INTO audit_log (event_id, event_type, actor, "
                    "occurred_at, request_id, agent_id, mandate_id, "
                    "idempotency_key, rule, reason, details, prev_hash, "
                    "entry_hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        event_id,
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
                        prev_hash,
                        entry_hash,
                    ),
                )
        except psycopg.Error as exc:
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
            "SELECT * FROM audit_log WHERE request_id = %s ORDER BY event_id",
            (request_id,),
        )

    def of_type(self, event_type: EventType) -> list[AuditEntry]:
        return self._query(
            "SELECT * FROM audit_log WHERE event_type = %s ORDER BY event_id",
            (event_type.value,),
        )

    def count_of(self, event_type: EventType, request_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM audit_log WHERE event_type = %s"
        params: tuple[Any, ...] = (event_type.value,)
        if request_id is not None:
            sql += " AND request_id = %s"
            params += (request_id,)
        with self.db.connection() as conn:
            return conn.execute(sql, params).fetchone()["n"]

    def timeline(self, request_id: str) -> str:
        """The log rendered for a human, with no access to the code."""
        entries = self.for_request(request_id)
        if not entries:
            return f"no audit entries for {request_id}"
        lines = [f"Request {request_id}"]
        lines += [f"  {e.describe()}" for e in entries]
        return "\n".join(lines)

    # -- integrity ---------------------------------------------------------

    def triggers(self) -> list[str]:
        """The append-only triggers as they exist in the live schema.

        Read from Postgres's catalog rather than repeated from this file, so
        a trigger someone dropped shows up missing instead of still described.
        """
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT pg_get_triggerdef(t.oid) AS definition "
                "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relname = 'audit_log' AND n.nspname = %s "
                "AND NOT t.tgisinternal ORDER BY t.tgname",
                (self.db.schema,),
            ).fetchall()
        return [r["definition"] for r in rows]

    def head(self) -> Optional[str]:
        """The newest entry's hash: the value worth recording elsewhere.

        A chain that only checks itself proves internal consistency, not that
        it is the same log as yesterday. Comparing this against a copy held
        somewhere the database's owner cannot silently edit is what makes a
        wholesale rewrite detectable.
        """
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT entry_hash FROM audit_log ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        return row["entry_hash"] if row else None

    def verify(self) -> ChainReport:
        """Recompute every link and report the first that does not hold.

        Reads the raw columns rather than `AuditEntry` objects, because
        `details` has to be hashed as the stored string -- see `chain_hash`.
        """
        with self.db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log ORDER BY event_id"
            ).fetchall()

        checked = 0
        prev_hash = GENESIS_HASH
        head: Optional[str] = None

        for row in rows:
            stored = row["entry_hash"]
            if row["prev_hash"] != prev_hash:
                return ChainReport(
                    intact=False, checked=checked, head=head,
                    broken_at=row["event_id"],
                    detail=(
                        f"entry {row['event_id']} expected to follow "
                        f"{prev_hash[:12]}… but records {str(row['prev_hash'])[:12]}… "
                        "— an entry was removed, reordered, or inserted"
                    ),
                )

            expected = chain_hash(
                prev_hash, event_id=row["event_id"],
                event_type=row["event_type"], actor=row["actor"],
                occurred_at=row["occurred_at"], request_id=row["request_id"],
                agent_id=row["agent_id"], mandate_id=row["mandate_id"],
                idempotency_key=row["idempotency_key"], rule=row["rule"],
                reason=row["reason"], details_json=row["details"],
            )
            if expected != stored:
                return ChainReport(
                    intact=False, checked=checked, head=head,
                    broken_at=row["event_id"],
                    detail=(
                        f"entry {row['event_id']} hashes to {expected[:12]}… "
                        f"but stores {stored[:12]}… — its contents were altered"
                    ),
                )

            checked += 1
            prev_hash = stored
            head = stored

        return ChainReport(intact=True, checked=checked, head=head)

    def _query(self, sql: str, params: tuple = ()) -> list[AuditEntry]:
        with self.db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_entry(r) for r in rows]


def _row_to_entry(row: dict) -> AuditEntry:
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
