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

from dataclasses import dataclass
from typing import Callable, Optional

from zerotrust.audit import Actor, AuditLog, EventType
from zerotrust.idempotency import IdempotencyStore, Outcome, Result
from zerotrust.policy import Decision, PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderTimeout

#: Every Phase 1 outcome maps to exactly one audit event. No outcome is
#: unlogged, and none produces two entries.
OUTCOME_EVENTS = {
    Outcome.EXECUTED: EventType.IDEMPOTENCY_EXECUTED,
    Outcome.REPLAYED: EventType.IDEMPOTENCY_REPLAYED,
    Outcome.RECLAIMED: EventType.IDEMPOTENCY_RECLAIMED,
    Outcome.IN_PROGRESS: EventType.IDEMPOTENCY_IN_PROGRESS,
    Outcome.CONFLICT: EventType.IDEMPOTENCY_CONFLICT,
    Outcome.AWAITING_VERIFICATION: EventType.PAYMENT_PENDING_VERIFICATION,
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
    ) -> None:
        self.policy = policy
        self.store = store
        self.audit = audit
        self._execute_purchase = execute_purchase

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

        try:
            result = self.store.execute(
                request.idempotency_key,
                request.payload(),
                logged_action,
                agent_id=request.agent_id,
            )
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
            self.store.mark_pending_verification(
                request.idempotency_key, str(exc), agent_id=request.agent_id)
            self._log(
                EventType.PAYMENT_PENDING_VERIFICATION,
                Actor.PROVIDER,
                mandate_id=decision.mandate_id,
                reason=str(exc),
                details={
                    "error_type": type(exc).__name__,
                    "velocity_slot": "held pending reconciliation",
                },
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

        if result.executed:
            self.policy.confirm_slot(request.agent_id, request.idempotency_key)
            self._log(
                EventType.PAYMENT_CAPTURED,
                Actor.PROVIDER,
                mandate_id=decision.mandate_id,
                details={
                    "amount_paise": request.amount_paise,
                    "response": result.response,
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

    def _log(self, event_type: EventType, actor: Actor, **kwargs) -> None:
        if self.audit is None:
            return
        self.audit.record(event_type, actor, **kwargs)
