"""Phase 7 — reconciliation: finding out what actually happened.

Every layer before this one could answer its question locally. This one cannot.
After an ambiguous failure the local ledger genuinely does not know whether
money moved, and no amount of reasoning about our own state can tell us. The
only authority is the provider, so reconciliation goes and asks.

THE RULE THAT MATTERS: never guess. Three outcomes are possible, and the third
is the one systems usually get wrong --

    provider has the order, we don't        -> DIVERGED, auto-repairable
    provider doesn't have it, we don't      -> nothing happened, safe to retry
    we cannot reach the provider            -> STILL UNKNOWN, stay pending

AND A FOURTH, learned the hard way against the real API: a provider's read
model can LAG. Razorpay's order list does not show an order that was created
seconds ago. So "the provider does not have it" is only trustworthy once
enough time has passed for the provider to have caught up. Inside that window
absence proves nothing, and treating it as proof would clear the way for a
second charge. `not_found_grace_seconds` is that window.

A system that collapses the third case into either of the first two is claiming
knowledge it does not have. That is how a payment gets silently double-charged
(assume it failed, retry) or silently lost (assume it succeeded, never retry).
`PENDING_VERIFICATION` exists so the honest answer has somewhere to live.

Auto-repair is deliberately narrow: exactly one matching order at the provider
is unambiguous, so the ledger is corrected. Anything else -- two orders for one
receipt, an amount that doesn't match -- is flagged for a human, because a
repair that guesses is worse than a divergence that is visible.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from zerotrust.audit import Actor, AuditLog, EventType
from zerotrust.idempotency import (
    COMPLETED,
    FAILED,
    PENDING_VERIFICATION,
    PROCESSING,
    IdempotencyStore,
)
from zerotrust.policy import PolicyEngine
from zerotrust.provider import PaymentProvider, ProviderError, ProviderTimeout

#: How long a provider's read model may lag before absence means anything.
#: Verified against Razorpay test mode, where a freshly created order is still
#: missing from the order list after 5 seconds.
DEFAULT_NOT_FOUND_GRACE_SECONDS = 300.0


class Finding(str, Enum):
    """What reconciliation concluded about one record."""

    #: Ledger and provider agree. The happy path -- and the case a noisy
    #: detector would wrongly flag, which is why it has its own test.
    CONSISTENT = "CONSISTENT"
    #: Provider executed it, our ledger didn't know. Repairable.
    DIVERGED_REPAIRED = "DIVERGED_REPAIRED"
    #: Provider never executed it. The failure was real; retrying is safe.
    CONFIRMED_NOT_EXECUTED = "CONFIRMED_NOT_EXECUTED"
    #: Genuinely ambiguous -- we could not reach the provider. Stays pending.
    STILL_UNKNOWN = "STILL_UNKNOWN"
    #: Diverged in a way no automatic repair can safely resolve.
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"


#: Findings after which the record is settled, one way or another.
RESOLVED_FINDINGS = frozenset({
    Finding.CONSISTENT,
    Finding.DIVERGED_REPAIRED,
    Finding.CONFIRMED_NOT_EXECUTED,
})


@dataclass(frozen=True)
class ReconcileResult:
    finding: Finding
    key: str
    agent_id: Optional[str] = None
    receipt: Optional[str] = None
    reason: str = ""
    provider_orders: list = field(default_factory=list)
    repaired_response: Optional[dict] = None

    @property
    def resolved(self) -> bool:
        return self.finding in RESOLVED_FINDINGS

    @property
    def needs_human(self) -> bool:
        return self.finding is Finding.NEEDS_HUMAN_REVIEW


class Reconciler:
    """Compares the local ledger against the provider, and repairs or flags."""

    def __init__(
        self,
        provider: PaymentProvider,
        store: IdempotencyStore,
        audit: Optional[AuditLog] = None,
        policy: Optional[PolicyEngine] = None,
        clock: Callable[[], float] = time.time,
        not_found_grace_seconds: float = DEFAULT_NOT_FOUND_GRACE_SECONDS,
    ) -> None:
        self.provider = provider
        self.store = store
        self.audit = audit
        self.policy = policy
        self._clock = clock
        self.not_found_grace_seconds = not_found_grace_seconds

    # -- the check ---------------------------------------------------------

    def reconcile(
        self,
        key: str,
        receipt: str,
        agent_id: Optional[str] = None,
        expected_amount_paise: Optional[int] = None,
        request_id: Optional[str] = None,
    ) -> ReconcileResult:
        """Resolve one record against the provider's truth."""
        record = self.store.get(key, agent_id=agent_id)
        local_status = record["status"] if record else None

        try:
            orders = self.provider.orders_for_receipt(receipt)
        except (ProviderTimeout, ProviderError) as exc:
            # We asked and did not get an answer. That is not permission to
            # decide; the record stays exactly as unknown as it was.
            result = ReconcileResult(
                finding=Finding.STILL_UNKNOWN,
                key=key, agent_id=agent_id, receipt=receipt,
                reason=(
                    f"could not reach the provider to verify: {exc}. The "
                    f"outcome remains unknown and the record stays pending."
                ),
            )
            self._log(EventType.PAYMENT_PENDING_VERIFICATION, Actor.SYSTEM,
                      result, request_id)
            return result

        # More than one order for a receipt that should have exactly one.
        # Automatic repair here would have to choose, and choosing is guessing.
        if len(orders) > 1:
            result = ReconcileResult(
                finding=Finding.NEEDS_HUMAN_REVIEW,
                key=key, agent_id=agent_id, receipt=receipt,
                provider_orders=orders,
                reason=(
                    f"{len(orders)} provider orders share receipt '{receipt}'; "
                    f"an automatic repair would have to guess which is real"
                ),
            )
            self._log(EventType.DIVERGENCE_DETECTED, Actor.SYSTEM, result,
                      request_id)
            return result

        if not orders:
            # Before concluding "it never happened", check the provider has
            # had time to catch up. Its read model lags; inside that window an
            # empty result is silence, not a denial.
            age = self._clock() - (record["claimed_at"] if record else 0)
            if record is not None and age < self.not_found_grace_seconds:
                result = ReconcileResult(
                    finding=Finding.STILL_UNKNOWN,
                    key=key, agent_id=agent_id, receipt=receipt,
                    reason=(
                        f"the provider reports no order for '{receipt}', but "
                        f"the attempt was only {age:.0f}s ago and the "
                        f"provider's read model lags; absence is not yet "
                        f"evidence (grace: {self.not_found_grace_seconds:.0f}s)"
                    ),
                )
                self._log(EventType.PAYMENT_PENDING_VERIFICATION, Actor.SYSTEM,
                          result, request_id)
                return result

            # The provider genuinely never executed it.
            if local_status == COMPLETED:
                # Our ledger claims a success the provider has never heard of.
                # Not auto-repairable: deleting a recorded success is exactly
                # the kind of "repair" that should require a human.
                result = ReconcileResult(
                    finding=Finding.NEEDS_HUMAN_REVIEW,
                    key=key, agent_id=agent_id, receipt=receipt,
                    reason=(
                        "the ledger records this as COMPLETED but the provider "
                        "has no such order; this needs a human, not an "
                        "automatic rewrite"
                    ),
                )
                self._log(EventType.DIVERGENCE_DETECTED, Actor.SYSTEM, result,
                          request_id)
                return result

            result = ReconcileResult(
                finding=Finding.CONFIRMED_NOT_EXECUTED,
                key=key, agent_id=agent_id, receipt=receipt,
                reason="the provider has no order for this receipt; the "
                       "failure was real and a retry is safe",
            )
            if local_status == PENDING_VERIFICATION:
                self.store.resolve_not_executed(key, agent_id=agent_id)
                self._release_slot(agent_id, key)
            self._log(EventType.DIVERGENCE_RESOLVED, Actor.SYSTEM, result,
                      request_id)
            return result

        # Exactly one order at the provider.
        order = orders[0]
        if (
            expected_amount_paise is not None
            and order.get("amount") != expected_amount_paise
        ):
            result = ReconcileResult(
                finding=Finding.NEEDS_HUMAN_REVIEW,
                key=key, agent_id=agent_id, receipt=receipt,
                provider_orders=orders,
                reason=(
                    f"provider order {order.get('id')} is for "
                    f"{order.get('amount')} paise, the ledger expected "
                    f"{expected_amount_paise}"
                ),
            )
            self._log(EventType.DIVERGENCE_DETECTED, Actor.SYSTEM, result,
                      request_id)
            return result

        if local_status == COMPLETED:
            # Both sides agree. A detector that cried wolf here would be worse
            # than useless, so this path is explicitly a non-event.
            return ReconcileResult(
                finding=Finding.CONSISTENT,
                key=key, agent_id=agent_id, receipt=receipt,
                provider_orders=orders,
                reason="ledger and provider agree",
            )

        # The divergence this phase exists for: the provider did it, we didn't
        # record it. Unambiguous, so repair.
        self._log(
            EventType.DIVERGENCE_DETECTED, Actor.SYSTEM,
            ReconcileResult(
                finding=Finding.DIVERGED_REPAIRED, key=key, agent_id=agent_id,
                receipt=receipt, provider_orders=orders,
                reason=(
                    f"provider holds order {order.get('id')} but the ledger "
                    f"status is {local_status}"
                ),
            ),
            request_id,
        )

        self.store.resolve_verified(key, order, agent_id=agent_id)
        self._confirm_slot(agent_id, key)

        result = ReconcileResult(
            finding=Finding.DIVERGED_REPAIRED,
            key=key, agent_id=agent_id, receipt=receipt,
            provider_orders=orders, repaired_response=order,
            reason=(
                f"ledger repaired from {local_status} to COMPLETED using "
                f"provider order {order.get('id')}"
            ),
        )
        self._log(EventType.DIVERGENCE_RESOLVED, Actor.SYSTEM, result,
                  request_id)
        return result

    def sweep(self, receipt_for: Callable[[str], str]) -> list[ReconcileResult]:
        """Reconcile every record whose outcome is still unknown.

        `receipt_for` maps a stored key to the receipt it used -- the caller
        owns that mapping, since only it knows how receipts were minted.
        """
        results = []
        for row in self.store.pending_verification():
            scoped = row["key"]
            agent_id, _, key = scoped.partition(":")
            if not key:  # unscoped key
                agent_id, key = None, scoped
            results.append(
                self.reconcile(key, receipt_for(scoped), agent_id=agent_id)
            )
        return results

    # -- helpers -----------------------------------------------------------

    def _confirm_slot(self, agent_id: Optional[str], key: str) -> None:
        if self.policy and agent_id:
            self.policy.confirm_slot(agent_id, key)

    def _release_slot(self, agent_id: Optional[str], key: str) -> None:
        if self.policy and agent_id:
            self.policy.release_slot(agent_id, key)

    def _log(self, event_type: EventType, actor: Actor,
             result: ReconcileResult, request_id: Optional[str]) -> None:
        if self.audit is None:
            return
        self.audit.record(
            event_type, actor,
            request_id=request_id,
            agent_id=result.agent_id,
            idempotency_key=result.key,
            reason=result.reason,
            details={
                "finding": result.finding.value,
                "receipt": result.receipt,
                "provider_order_ids": [o.get("id") for o in result.provider_orders],
            },
        )


@dataclass
class SweepCycle:
    """What one pass of the scheduler did."""

    started_at: float
    finished_at: float
    records_seen: int
    findings: dict = field(default_factory=dict)
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "records_seen": self.records_seen,
            "findings": self.findings,
            "error": self.error,
        }


class ReconciliationScheduler:
    """Runs `Reconciler.sweep()` on an interval, so nothing waits for a human.

    Phase 7 built detection and repair but never scheduled them, which meant a
    record whose outcome was unknown sat frozen until somebody thought to look.
    The purchase was safe -- retries are refused -- but it was also stuck, and
    its velocity slot stayed held.

    Two properties matter more than the timing:

    NEVER OVERLAPPING. One sweep runs at a time. A provider that answers slowly
    must not cause sweeps to stack up behind each other, each re-reconciling
    records the last one is still working through.

    NEVER FATAL. An exception inside a cycle is recorded and the loop
    continues. A scheduler that dies on the first provider blip is worse than
    no scheduler, because its absence is silent -- everything looks fine while
    nothing is being reconciled.
    """

    def __init__(
        self,
        reconciler: "Reconciler",
        receipt_for: Callable[[str], str],
        interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.reconciler = reconciler
        self.receipt_for = receipt_for
        self.interval_seconds = interval_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.cycles: list[SweepCycle] = []

    # -- one pass ----------------------------------------------------------

    def run_once(self) -> SweepCycle:
        """Reconcile everything currently pending. Safe to call directly."""
        started = self._clock()
        findings: dict = {}
        error = None
        seen = 0
        try:
            results = self.reconciler.sweep(self.receipt_for)
            seen = len(results)
            for result in results:
                key = result.finding.value
                findings[key] = findings.get(key, 0) + 1
        except Exception as exc:  # noqa: BLE001 - deliberately broad; see above
            error = f"{type(exc).__name__}: {exc}"

        cycle = SweepCycle(started_at=started, finished_at=self._clock(),
                           records_seen=seen, findings=findings, error=error)
        with self._lock:
            self.cycles.append(cycle)
        return cycle

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            # Event.wait doubles as the sleep and the shutdown signal, so
            # stopping does not have to wait out a full interval.
            self._stop.wait(self.interval_seconds)

    def start(self) -> "ReconciliationScheduler":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="reconciliation-sweep")
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def __enter__(self) -> "ReconciliationScheduler":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # -- what it has done --------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            cycles = list(self.cycles)
        totals: dict = {}
        for cycle in cycles:
            for finding, count in cycle.findings.items():
                totals[finding] = totals.get(finding, 0) + count
        return {
            "running": self.running,
            "interval_seconds": self.interval_seconds,
            "cycles": len(cycles),
            "last_cycle": cycles[-1].as_dict() if cycles else None,
            "records_resolved": totals,
            "errors": sum(1 for c in cycles if c.error),
        }
