"""Phase 3 — the authorization boundary, wired end to end.

This is the single door a purchase goes through, and the order of operations is
the whole point:

    policy check  ->  idempotent execution  ->  provider

Policy comes FIRST. A denied request never reaches the idempotency layer and
never touches the provider -- verified by asserting zero provider calls on
denial, not by reading the code and assuming.

The gateway owns nothing itself. It composes the Phase 1 store, the Phase 3
policy engine, and a Phase 2 provider, which is why each can still be tested in
isolation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from zerotrust.idempotency import IdempotencyStore, Outcome, Result
from zerotrust.policy import Decision, PolicyEngine, PurchaseRequest


@dataclass(frozen=True)
class PurchaseOutcome:
    """What happened to a purchase request, end to end."""

    decision: Decision
    result: Optional[Result] = None

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
        return self.decision.reason

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
    ) -> None:
        self.policy = policy
        self.store = store
        self._execute_purchase = execute_purchase

    def submit(self, request: PurchaseRequest) -> PurchaseOutcome:
        decision = self.policy.evaluate(request)
        if decision.denied:
            # Terminal. No idempotency record, no provider call, nothing.
            return PurchaseOutcome(decision=decision)

        try:
            result = self.store.execute(
                request.idempotency_key,
                request.payload(),
                lambda: self._execute_purchase(request),
                agent_id=request.agent_id,
            )
        except Exception:
            # Execution blew up: hand the velocity slot back so a failed
            # attempt doesn't silently consume the agent's budget.
            self.policy.release_slot(request.agent_id, request.idempotency_key)
            raise

        if result.executed:
            self.policy.confirm_slot(request.agent_id, request.idempotency_key)

        return PurchaseOutcome(decision=decision, result=result)
