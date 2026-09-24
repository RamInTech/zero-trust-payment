"""Phase 3 + 4 — the authorization boundary, wired end to end.

Order of operations, and it is the whole point:

    audit(intent)  ->  policy check  ->  audit(decision)
                   ->  audit(attempt)  ->  idempotent execution  ->  audit(outcome)

Two rules are encoded here that are easy to state and easy to get wrong:

POLICY COMES FIRST. A denied request never reaches the idempotency layer and
never touches the provider -- asserted by counting provider calls on denial,
not by reading the code and trusting the ordering.

THE LOG IS WRITTEN BEFORE THE MONEY MOVES. `AuditWriteError` propagates and
blocks execution, rather than a payment happening with no record of it. The
cost is honest and accepted: an intent can be logged whose outcome is then
unknown (a crash between the two writes), which is a gap Phase 7's
reconciliation closes. That is strictly better than the reverse failure, where
money moves and nothing knows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from zerotrust.audit import Actor, AuditLog, EventType
from zerotrust.idempotency import (
    CompletionNotRecorded,
    IdempotencyStore,
    Outcome,
    Result,
    StaleVerdict,
    scope_key,
)
from zerotrust.ledger import Ledger, require_shared_database
from zerotrust.policy import Decision, PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderError, ProviderTimeout
# How long the provider's order list may lag behind a charge. Reconciliation
# asks the same question for the same reason, so they share one value.
from zerotrust.reconcile import DEFAULT_NOT_FOUND_GRACE_SECONDS

#: Every Phase 1 outcome maps to exactly one audit event. No outcome is
#: unlogged, and none produces two entries.
OUTCOME_EVENTS = {
    Outcome.EXECUTED: EventType.IDEMPOTENCY_EXECUTED,
    Outcome.REPLAYED: EventType.IDEMPOTENCY_REPLAYED,
    Outcome.RECLAIMED: EventType.IDEMPOTENCY_RECLAIMED,
    Outcome.IN_PROGRESS: EventType.IDEMPOTENCY_IN_PROGRESS,
    Outcome.CONFLICT: EventType.IDEMPOTENCY_CONFLICT,
    Outcome.AWAITING_VERIFICATION: EventType.PAYMENT_PENDING_VERIFICATION,
    Outcome.RECOVERED: EventType.IDEMPOTENCY_RECOVERED,
}



@dataclass(frozen=True)
class PurchaseOutcome:
    """What happened to a purchase request, end to end."""

    decision: Decision
    result: Optional[Result] = None
    request_id: Optional[str] = None

    @property
    def approved(self) -> bool:
        return self.decision.approved

    @property
    def denied(self) -> bool:
        return self.decision.denied

    @property
    def rule(self):
        return self.decision.rule

    @property
    def reason(self) -> Optional[str]:
        return self.decision.reason or (self.result.reason if self.result else None)

    @property
    def outcome(self) -> Optional[Outcome]:
        return self.result.outcome if self.result else None

    @property
    def response(self) -> Optional[dict]:
        return self.result.response if self.result else None

    @property
    def executed(self) -> bool:
        """True only if the money action actually ran on this call."""
        return bool(self.result and self.result.executed)


class PurchaseGateway:
    def __init__(
        self,
        policy: PolicyEngine,
        store: IdempotencyStore,
        execute_purchase: Callable[[PurchaseRequest], dict],
        audit: Optional[AuditLog] = None,
        ledger: Optional[Ledger] = None,
        find_orders: Optional[Callable[[PurchaseRequest], list]] = None,
        not_found_grace_seconds: float = DEFAULT_NOT_FOUND_GRACE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.policy = policy
        self.store = store
        self.audit = audit
        #: Optional so every caller that predates the books keeps working. When
        #: present, each posting commits in the same transaction as the record
        #: it mirrors -- the sale with COMPLETED, suspense with the freeze --
        #: so the books and the idempotency store cannot disagree about a
        #: purchase that finished. That needs one Database, checked here.
        self.ledger = ledger
        require_shared_database(ledger, store.db, "PurchaseGateway")
        self._execute_purchase = execute_purchase
        #: Phase 12. Asks the provider which orders exist for this purchase.
        #: With it, a stale key is checked before it is re-run: a claimant
        #: that charged and then died is completed from its order instead of
        #: charged again. Without it, a stale reclaim re-runs the purchase --
        #: correct only if the stalled claimant never reached the provider.
        self._find_orders = find_orders
        self.not_found_grace_seconds = not_found_grace_seconds
        self._clock = clock

    def submit(
        self, request: PurchaseRequest, request_id: Optional[str] = None
    ) -> PurchaseOutcome:
        # A caller that supplies its own request_id has already logged the
        # intent (see CheckoutService.propose). Logging it again here would
        # put two PURCHASE_REQUESTED entries on one request and break Phase
        # 4's "exactly one entry per outcome" property.
        originated_here = request_id is None
        request_id = request_id or (
            AuditLog.new_request_id() if self.audit else None
        )
        common = dict(
            request_id=request_id,
            agent_id=request.agent_id,
            idempotency_key=request.idempotency_key,
        )

        if originated_here:
            self._log(
                EventType.PURCHASE_REQUESTED,
                Actor.AGENT,  # the agent proposes; it does not authorise
                details={
                    "sku": request.sku,
                    "amount_paise": request.amount_paise,
                    "currency": request.currency,
                },
                **common,
            )

        decision = self.policy.evaluate(request)

        if decision.denied:
            self._log(
                EventType.POLICY_DENIED,
                Actor.POLICY_ENGINE,
                mandate_id=decision.mandate_id,
                rule=decision.rule.value if decision.rule else None,
                reason=decision.reason,
                details=decision.details,
                **common,
            )
            # Terminal. No idempotency record, no provider call, nothing.
            return PurchaseOutcome(decision=decision, request_id=request_id)

        self._log(
            EventType.POLICY_APPROVED,
            Actor.POLICY_ENGINE,
            mandate_id=decision.mandate_id,
            reason="all mandate rules satisfied",
            details=decision.details,
            **common,
        )

        def logged_action() -> dict:
            # Written BEFORE the money moves, deliberately.
            self._log(
                EventType.PAYMENT_ATTEMPTED,
                Actor.SYSTEM,
                mandate_id=decision.mandate_id,
                details={"amount_paise": request.amount_paise, "sku": request.sku},
                **common,
            )
            return self._execute_purchase(request)

        scoped = scope_key(request.idempotency_key, request.agent_id)

        def book_sale(conn, attempts: int) -> None:
            # Runs inside the transaction that marks the key COMPLETED.
            self.ledger.record_sale(
                scoped, request.amount_paise, attempt=attempts,
                agent_id=request.agent_id, request_id=request_id, conn=conn)

        def book_suspense(conn, attempts: int) -> None:
            # Suspense, not revenue: the money may or may not have moved.
            # A no-op if a reconciler already settled this attempt.
            self.ledger.record_unverified(
                scoped, request.amount_paise, attempt=attempts,
                agent_id=request.agent_id, request_id=request_id, conn=conn)

        try:
            result = self.store.execute(
                request.idempotency_key,
                request.payload(),
                logged_action,
                agent_id=request.agent_id,
                also=book_sale if self.ledger is not None else None,
                verify_stale=(self._stale_verifier(request)
                              if self._find_orders is not None else None),
                also_on_freeze=book_suspense if self.ledger is not None else None,
            )
        except CompletionNotRecorded as exc:
            # The provider succeeded; the COMPLETED record and its sale could
            # not be written, and neither was. The store froze the key, so no
            # retry can charge again, and the slot stays held: money moved.
            # Reconciliation books the sale. Until then, suspense says so.
            suspense = "not attempted: the record could not be frozen either"
            if self.ledger is not None and exc.attempts is not None:
                try:
                    book_suspense_alone = self.ledger.record_unverified(
                        scoped, request.amount_paise, attempt=exc.attempts,
                        agent_id=request.agent_id, request_id=request_id)
                    suspense = f"posted as transaction {book_suspense_alone}"
                except Exception as posting_exc:  # noqa: BLE001
                    suspense = f"failed: {posting_exc}"
            self._log(
                EventType.PAYMENT_PENDING_VERIFICATION,
                Actor.SYSTEM,
                mandate_id=decision.mandate_id,
                reason=str(exc),
                details={
                    "error_type": type(exc.cause).__name__,
                    "velocity_slot": "held pending reconciliation",
                    "suspense": suspense,
                },
                **common,
            )
            raise exc.cause from exc
        except ProviderTimeout as exc:
            # The outcome is UNKNOWN, not failed. Two things follow, and both
            # are deliberate:
            #
            #  1. The record is frozen as PENDING_VERIFICATION, so a retry is
            #     refused rather than re-executed. Retrying an unknown outcome
            #     is exactly how a timeout becomes a double charge.
            #  2. The velocity slot is HELD, not released. Releasing it would
            #     let an agent manufacture extra budget by inducing timeouts.
            #     Reconciliation releases it if the purchase never happened.
            #
            # The freeze and the suspense posting commit together. If the
            # posting fails the store still freezes the key on its own -- the
            # freeze is what prevents a double charge -- and the missing
            # posting shows in the audit entry and in `discrepancies()`.
            details = {
                "error_type": type(exc).__name__,
                "velocity_slot": "held pending reconciliation",
            }
            try:
                self.store.mark_pending_verification(
                    request.idempotency_key, str(exc), agent_id=request.agent_id,
                    also=book_suspense if self.ledger is not None else None)
            except KeyError:
                raise
            except Exception as posting_exc:  # noqa: BLE001
                details["suspense"] = f"failed: {posting_exc}"
            self._log(
                EventType.PAYMENT_PENDING_VERIFICATION,
                Actor.PROVIDER,
                mandate_id=decision.mandate_id,
                reason=str(exc),
                details=details,
                **common,
            )
            raise
        except Exception as exc:
            self._log(
                EventType.PAYMENT_FAILED,
                Actor.PROVIDER,
                mandate_id=decision.mandate_id,
                reason=str(exc),
                details={"error_type": type(exc).__name__},
                **common,
            )
            # A definite failure: the provider was reached and said no, or we
            # never got that far. Hand the velocity slot back.
            self.policy.release_slot(request.agent_id, request.idempotency_key)
            raise

        self._log(
            OUTCOME_EVENTS[result.outcome],
            Actor.SYSTEM,
            mandate_id=decision.mandate_id,
            reason=result.reason,
            details={"attempts": result.attempts},
            **common,
        )

        recovered = result.outcome is Outcome.RECOVERED
        if result.executed or recovered:
            # A recovered key did charge -- in the attempt that died -- so its
            # budget is spent and its capture belongs in the record.
            self.policy.confirm_slot(request.agent_id, request.idempotency_key)
            self._log(
                EventType.PAYMENT_CAPTURED,
                Actor.PROVIDER,
                mandate_id=decision.mandate_id,
                details={
                    "amount_paise": request.amount_paise,
                    "response": result.response,
                    "recovered_from_stalled_attempt": recovered,
                    # Capture is simulated -- see zerotrust/provider.py.
                    "simulated": bool(
                        isinstance(result.response, dict)
                        and result.response.get("simulated")
                    ),
                },
                **common,
            )

        return PurchaseOutcome(
            decision=decision, result=result, request_id=request_id
        )

    def _stale_verifier(self, request: PurchaseRequest):
        """Build the question asked before a stale key is re-run.

        Each answer maps to the one safe action for it. The asymmetry is
        deliberate: only a clear "no order, and long enough ago to be sure"
        permits running the purchase again. Everything ambiguous freezes.
        """
        def verify(claim: Result) -> StaleVerdict:
            try:
                orders = list(self._find_orders(request))
            except (ProviderTimeout, ProviderError) as exc:
                return StaleVerdict.unknown(
                    f"could not ask the provider whether the stalled attempt "
                    f"charged ({exc}); the key is frozen rather than re-run")
            if len(orders) > 1:
                return StaleVerdict.unknown(
                    f"{len(orders)} provider orders already exist for this "
                    f"purchase; choosing between them needs a human")
            if len(orders) == 1:
                order = orders[0]
                if order.get("amount") != request.amount_paise:
                    return StaleVerdict.unknown(
                        f"the provider's order is for {order.get('amount')} "
                        f"paise but this purchase is for "
                        f"{request.amount_paise}; not completing from it")
                return StaleVerdict.executed(order)
            age = self._clock() - (claim.prior_claimed_at or 0.0)
            if age < self.not_found_grace_seconds:
                return StaleVerdict.unknown(
                    f"the provider shows no order yet, but the stalled attempt "
                    f"began {age:.0f}s ago and its order list can lag by up to "
                    f"{self.not_found_grace_seconds:.0f}s; absence is not yet "
                    f"evidence")
            return StaleVerdict.not_executed()
        return verify

    def _log(self, event_type: EventType, actor: Actor, **kwargs) -> None:
        if self.audit is None:
            return
        self.audit.record(event_type, actor, **kwargs)
