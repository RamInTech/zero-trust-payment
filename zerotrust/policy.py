"""Phase 3 — the policy engine.

Every purchase request is checked against the agent's mandate BEFORE the
idempotency/execution layer is ever invoked. A denial names the exact rule that
was broken; a generic "denied" is treated as a bug here, because an
unexplainable denial undermines the whole "auditable" claim.

VELOCITY IS THE HARD PART. The other three checks (amount, SKU, expiry) are
pure comparisons against fields on the mandate -- no shared state, no races.
Velocity is different: it depends on how many purchases already happened, so
two concurrent requests can both read "2 used, cap is 3" and both proceed,
putting 4 through a cap of 3. Read-then-act is unsafe under concurrency, which
is exactly the lesson of Phase 1.

So a velocity slot is CLAIMED, not counted: the count and the insert happen in
one BEGIN IMMEDIATE transaction, and SQLite serialises writers. And the claim
table carries UNIQUE(agent_id, idempotency_key), so a retry of an existing
request reuses its own slot instead of consuming a second one -- the same
unique-constraint trick Phase 1 uses, applied one level up.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from zerotrust.mandate import Mandate, MandateStore

# Slot lifecycle.
SLOT_HELD = "HELD"          # claimed, execution in flight -- still counts
SLOT_CONFIRMED = "CONFIRMED"  # purchase completed -- counts
SLOT_RELEASED = "RELEASED"  # execution failed -- does NOT count


class Rule(str, Enum):
    """The specific rule a denial cites. Never a generic failure."""

    MALFORMED_REQUEST = "MALFORMED_REQUEST"
    NO_ACTIVE_MANDATE = "NO_ACTIVE_MANDATE"
    MANDATE_EXPIRED = "MANDATE_EXPIRED"
    AMOUNT_EXCEEDS_CAP = "AMOUNT_EXCEEDS_CAP"
    SKU_NOT_ALLOWED = "SKU_NOT_ALLOWED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    VELOCITY_EXCEEDED = "VELOCITY_EXCEEDED"


@dataclass(frozen=True)
class PurchaseRequest:
    agent_id: str
    sku: str
    amount_paise: int
    idempotency_key: str
    currency: str = "INR"

    def payload(self) -> dict:
        """The canonical payload the idempotency layer fingerprints."""
        return {
            "agent_id": self.agent_id,
            "sku": self.sku,
            "amount_paise": self.amount_paise,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class Decision:
    approved: bool
    request: PurchaseRequest
    rule: Optional[Rule] = None
    reason: Optional[str] = None
    mandate_id: Optional[str] = None
    details: dict = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return not self.approved


_SCHEMA = """
CREATE TABLE IF NOT EXISTS velocity_slots (
    slot_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        TEXT NOT NULL,
    mandate_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    sku             TEXT NOT NULL,
    amount_paise    INTEGER NOT NULL,
    status          TEXT NOT NULL,
    claimed_at      REAL NOT NULL,
    -- one slot per request, so a retry cannot consume a second one
    UNIQUE (agent_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_slots_agent_time
    ON velocity_slots(agent_id, claimed_at);
"""


class PolicyEngine:
    def __init__(
        self,
        mandate_store: MandateStore,
        db_path: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.mandates = mandate_store
        self.db_path = db_path or mandate_store.db_path
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

    # -- the checks --------------------------------------------------------

    def evaluate(self, request: PurchaseRequest) -> Decision:
        """Check a request against its mandate. Claims a velocity slot on approval.

        Order matters: malformed input is rejected before any mandate lookup
        (a negative amount is not a policy question), and velocity is checked
        last because it is the only check with a side effect.
        """
        now = self._clock()

        malformed = self._check_malformed(request)
        if malformed:
            return malformed

        mandate = self.mandates.active_for_agent(request.agent_id)
        if mandate is None:
            return self._deny(
                request,
                Rule.NO_ACTIVE_MANDATE,
                f"agent '{request.agent_id}' has no active mandate",
            )

        for check in (self._check_expiry, self._check_amount,
                      self._check_sku, self._check_currency):
            denial = check(request, mandate, now)
            if denial:
                return denial

        return self._claim_velocity_slot(request, mandate, now)

    def _check_malformed(self, request: PurchaseRequest) -> Optional[Decision]:
        if not isinstance(request.amount_paise, int) or isinstance(
            request.amount_paise, bool
        ):
            return self._deny(
                request,
                Rule.MALFORMED_REQUEST,
                "amount_paise must be an integer number of paise",
            )
        if request.amount_paise <= 0:
            return self._deny(
                request,
                Rule.MALFORMED_REQUEST,
                f"amount_paise must be positive, got {request.amount_paise}",
                amount_paise=request.amount_paise,
            )
        if not request.sku:
            return self._deny(request, Rule.MALFORMED_REQUEST, "sku is required")
        if not request.idempotency_key:
            return self._deny(
                request, Rule.MALFORMED_REQUEST, "idempotency_key is required"
            )
        return None

    def _check_expiry(
        self, request: PurchaseRequest, mandate: Mandate, now: float
    ) -> Optional[Decision]:
        if mandate.is_expired(now):
            return self._deny(
                request,
                Rule.MANDATE_EXPIRED,
                f"mandate {mandate.mandate_id} expired "
                f"{now - mandate.expires_at:.0f}s ago",
                mandate_id=mandate.mandate_id,
                expires_at=mandate.expires_at,
                now=now,
            )
        return None

    def _check_amount(
        self, request: PurchaseRequest, mandate: Mandate, now: float
    ) -> Optional[Decision]:
        if request.amount_paise > mandate.max_amount_paise:
            return self._deny(
                request,
                Rule.AMOUNT_EXCEEDS_CAP,
                f"amount {request.amount_paise} paise exceeds the per-transaction "
                f"cap of {mandate.max_amount_paise} paise",
                mandate_id=mandate.mandate_id,
                requested_paise=request.amount_paise,
                cap_paise=mandate.max_amount_paise,
            )
        return None

    def _check_sku(
        self, request: PurchaseRequest, mandate: Mandate, now: float
    ) -> Optional[Decision]:
        if not mandate.allows_sku(request.sku):
            return self._deny(
                request,
                Rule.SKU_NOT_ALLOWED,
                f"sku '{request.sku}' is not in the mandate's allowlist",
                mandate_id=mandate.mandate_id,
                requested_sku=request.sku,
                allowed_skus=sorted(mandate.allowed_skus),
            )
        return None

    def _check_currency(
        self, request: PurchaseRequest, mandate: Mandate, now: float
    ) -> Optional[Decision]:
        if request.currency != mandate.currency:
            return self._deny(
                request,
                Rule.CURRENCY_MISMATCH,
                f"currency '{request.currency}' does not match the mandate "
                f"currency '{mandate.currency}'",
                mandate_id=mandate.mandate_id,
                requested_currency=request.currency,
                mandate_currency=mandate.currency,
            )
        return None

    # -- velocity: claimed, not counted ------------------------------------

    def _claim_velocity_slot(
        self, request: PurchaseRequest, mandate: Mandate, now: float
    ) -> Decision:
        window_start = now - mandate.velocity_window_secs
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")

            existing = conn.execute(
                "SELECT * FROM velocity_slots WHERE agent_id = ? AND "
                "idempotency_key = ?",
                (request.agent_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                # A retry of a request that already holds a slot. Reusing it is
                # what stops retries from eating the agent's velocity budget.
                conn.execute("COMMIT")
                return Decision(
                    approved=True,
                    request=request,
                    mandate_id=mandate.mandate_id,
                    details={
                        "velocity_slot": "reused",
                        "slot_status": existing["status"],
                    },
                )

            used = conn.execute(
                "SELECT COUNT(*) AS n FROM velocity_slots WHERE agent_id = ? "
                "AND status IN (?, ?) AND claimed_at >= ?",
                (request.agent_id, SLOT_HELD, SLOT_CONFIRMED, window_start),
            ).fetchone()["n"]

            if used >= mandate.velocity_limit:
                conn.execute("COMMIT")
                window_mins = mandate.velocity_window_secs / 60
                return self._deny(
                    request,
                    Rule.VELOCITY_EXCEEDED,
                    f"velocity limit reached: {used} of {mandate.velocity_limit} "
                    f"purchases already made in the last "
                    f"{window_mins:.0f} minute(s)",
                    mandate_id=mandate.mandate_id,
                    used=used,
                    limit=mandate.velocity_limit,
                    window_secs=mandate.velocity_window_secs,
                )

            conn.execute(
                "INSERT INTO velocity_slots (agent_id, mandate_id, "
                "idempotency_key, sku, amount_paise, status, claimed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request.agent_id,
                    mandate.mandate_id,
                    request.idempotency_key,
                    request.sku,
                    request.amount_paise,
                    SLOT_HELD,
                    now,
                ),
            )
            conn.execute("COMMIT")
            return Decision(
                approved=True,
                request=request,
                mandate_id=mandate.mandate_id,
                details={
                    "velocity_slot": "claimed",
                    "used_before": used,
                    "limit": mandate.velocity_limit,
                },
            )
        finally:
            conn.close()

    def confirm_slot(self, agent_id: str, idempotency_key: str) -> None:
        self._set_slot_status(agent_id, idempotency_key, SLOT_CONFIRMED)

    def release_slot(self, agent_id: str, idempotency_key: str) -> None:
        """Give a slot back when execution failed, so a failure costs no budget."""
        self._set_slot_status(agent_id, idempotency_key, SLOT_RELEASED)

    def _set_slot_status(self, agent_id: str, key: str, status: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE velocity_slots SET status = ? WHERE agent_id = ? "
                "AND idempotency_key = ?",
                (status, agent_id, key),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()

    def slots_used(self, agent_id: str, window_secs: float) -> int:
        conn = self._connect()
        try:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM velocity_slots WHERE agent_id = ? "
                "AND status IN (?, ?) AND claimed_at >= ?",
                (agent_id, SLOT_HELD, SLOT_CONFIRMED, self._clock() - window_secs),
            ).fetchone()["n"]
        finally:
            conn.close()

    # -- helper ------------------------------------------------------------

    def _deny(
        self,
        request: PurchaseRequest,
        rule: Rule,
        reason: str,
        mandate_id: Optional[str] = None,
        **details,
    ) -> Decision:
        return Decision(
            approved=False,
            request=request,
            rule=rule,
            reason=reason,
            mandate_id=mandate_id,
            details=details,
        )
