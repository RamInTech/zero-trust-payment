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
one transaction holding a lock on that agent's budget. And the claim table
carries UNIQUE(agent_id, idempotency_key), so a retry of an existing request
reuses its own slot instead of consuming a second one -- the same
unique-constraint trick Phase 1 uses, applied one level up.

Phase 10 (Postgres): SQLite serialised every writer to the file, and the claim
leaned on that. Postgres does not, so the claim takes an advisory lock named
after the agent. Two agents no longer wait for each other; two requests from
the same agent still cannot both read the count before either inserts.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from zerotrust.db import Database
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
    #: The agent has been denied too often, too fast. Refused before the
    #: mandate rules are evaluated -- an agent grinding against the policy
    #: engine is throttled rather than merely denied over and over.
    COOLDOWN_ACTIVE = "COOLDOWN_ACTIVE"


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
    slot_id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    mandate_id      TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    sku             TEXT NOT NULL,
    amount_paise    BIGINT NOT NULL,
    status          TEXT NOT NULL,
    claimed_at      DOUBLE PRECISION NOT NULL,
    -- one slot per request, so a retry cannot consume a second one
    UNIQUE (agent_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_slots_agent_time
    ON velocity_slots(agent_id, claimed_at);

CREATE TABLE IF NOT EXISTS denials (
    denial_id  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id   TEXT NOT NULL,
    rule       TEXT NOT NULL,
    denied_at  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_denials_agent_time
    ON denials(agent_id, denied_at);
"""


class PolicyEngine:
    def __init__(
        self,
        mandate_store: MandateStore,
        db: Optional[Database] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.mandates = mandate_store
        self.db = db or mandate_store.db
        self._clock = clock
        self.db.apply_schema(_SCHEMA)

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

        # Throttling comes before the rules: an agent already in cool-down is
        # refused without the engine bothering to evaluate the request.
        cooling = self._check_cooldown(request, mandate, now)
        if cooling:
            return cooling

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
        with self.db.transaction() as conn:
            self.db.lock(conn, f"velocity:{request.agent_id}")

            existing = conn.execute(
                "SELECT * FROM velocity_slots WHERE agent_id = %s AND "
                "idempotency_key = %s",
                (request.agent_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                # A retry of a request that already holds a slot. Reusing it is
                # what stops retries from eating the agent's velocity budget.
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
                "SELECT COUNT(*) AS n FROM velocity_slots WHERE agent_id = %s "
                "AND status IN (%s, %s) AND claimed_at >= %s",
                (request.agent_id, SLOT_HELD, SLOT_CONFIRMED, window_start),
            ).fetchone()["n"]

            over_limit = used >= mandate.velocity_limit
            if not over_limit:
                conn.execute(
                    "INSERT INTO velocity_slots (agent_id, mandate_id, "
                    "idempotency_key, sku, amount_paise, status, claimed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
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

        if over_limit:
            # Recorded after the lock is released: a denial needs no hold on
            # the budget, and taking a second connection while holding the
            # lock would make every refusal wait on the pool.
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

    def confirm_slot(self, agent_id: str, idempotency_key: str) -> None:
        self._set_slot_status(agent_id, idempotency_key, SLOT_CONFIRMED)

    def release_slot(self, agent_id: str, idempotency_key: str) -> None:
        """Give a slot back when execution failed, so a failure costs no budget."""
        self._set_slot_status(agent_id, idempotency_key, SLOT_RELEASED)

    def _set_slot_status(self, agent_id: str, key: str, status: str) -> None:
        with self.db.connection() as conn:
            conn.execute(
                "UPDATE velocity_slots SET status = %s WHERE agent_id = %s "
                "AND idempotency_key = %s",
                (status, agent_id, key),
            )

    def slots_used(self, agent_id: str, window_secs: float) -> int:
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM velocity_slots WHERE agent_id = %s "
                "AND status IN (%s, %s) AND claimed_at >= %s",
                (agent_id, SLOT_HELD, SLOT_CONFIRMED, self._clock() - window_secs),
            ).fetchone()["n"]

    # -- helper ------------------------------------------------------------

    def _record_denial(self, agent_id: str, rule: Rule) -> None:
        """Remember a denial, for the cool-down count.

        Kept in the policy engine's own tables rather than read back out of
        the audit log: the engine knows nothing about the audit log today, and
        coupling it to one so it can rate-limit would be a strange dependency
        for a component whose job is to decide, not to remember.
        """
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO denials (agent_id, rule, denied_at) VALUES (%s, %s, %s)",
                (agent_id, rule.value, self._clock()),
            )

    def denials_in_window(self, agent_id: str, window_secs: float) -> int:
        """Denials that count toward the cool-down.

        COOLDOWN_ACTIVE denials are excluded, and that exclusion is what makes
        the throttle terminate. Counting them would mean every refusal renewed
        the window, so an agent that hit the threshold once could never leave
        it -- a permanent ban wearing a rate limit's clothes.
        """
        with self.db.connection() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM denials WHERE agent_id = %s "
                "AND rule != %s AND denied_at >= %s",
                (agent_id, Rule.COOLDOWN_ACTIVE.value,
                 self._clock() - window_secs),
            ).fetchone()["n"]

    def _check_cooldown(
        self, request: PurchaseRequest, mandate: Mandate, now: float
    ) -> Optional[Decision]:
        if mandate.cooldown_denials <= 0:
            return None
        used = self.denials_in_window(request.agent_id,
                                      mandate.cooldown_window_secs)
        if used < mandate.cooldown_denials:
            return None

        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT MIN(denied_at) AS oldest FROM denials WHERE agent_id = %s "
                "AND rule != %s AND denied_at >= %s",
                (request.agent_id, Rule.COOLDOWN_ACTIVE.value,
                 now - mandate.cooldown_window_secs),
            ).fetchone()
        oldest = row["oldest"] if row and row["oldest"] else now
        retry_after = max(0.0, (oldest + mandate.cooldown_window_secs) - now)

        return self._deny(
            request,
            Rule.COOLDOWN_ACTIVE,
            f"agent is in cool-down: {used} denials in the last "
            f"{mandate.cooldown_window_secs / 60:.0f} minute(s); "
            f"retry in {retry_after:.0f}s",
            mandate_id=mandate.mandate_id,
            denials=used,
            threshold=mandate.cooldown_denials,
            window_secs=mandate.cooldown_window_secs,
            retry_after_secs=retry_after,
        )

    def _deny(
        self,
        request: PurchaseRequest,
        rule: Rule,
        reason: str,
        mandate_id: Optional[str] = None,
        **details,
    ) -> Decision:
        self._record_denial(request.agent_id, rule)
        return Decision(
            approved=False,
            request=request,
            rule=rule,
            reason=reason,
            mandate_id=mandate_id,
            details=details,
        )
