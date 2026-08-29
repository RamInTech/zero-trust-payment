"""Phase 7 completion test — Failure Injection & Recovery.

The failure being induced: a payment that SUCCEEDS at the provider whose
confirmation write to the local ledger fails. The two sides now disagree, and
only the provider knows the truth.

The property that matters most is not that divergence is detected -- it is that
an outcome we cannot determine is never forced into one we can. A system that
guesses "probably failed" double-charges; one that guesses "probably succeeded"
loses payments silently.
"""

from __future__ import annotations

import pytest

from zerotrust.audit import AuditLog, EventType
from zerotrust.faults import Fault, FaultInjector, InjectedCrash
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import (
    COMPLETED,
    FAILED,
    PENDING_VERIFICATION,
    IdempotencyStore,
    Outcome,
)
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest
from zerotrust.provider import ProviderError, ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import (
    DEFAULT_NOT_FOUND_GRACE_SECONDS,
    Finding,
    Reconciler,
)

HOUR = 3600.0
AGENT = "agent_1"
RECEIPT = "rcpt_phase7"


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def provider():
    return SimulatedProvider()


@pytest.fixture
def faults():
    return FaultInjector()


@pytest.fixture
def stack(tmp_path, clock, provider, faults):
    """A full stack whose executor can be made to fail on demand."""
    audit = AuditLog(str(tmp_path / "audit.db"), clock=clock)
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db"), clock=clock),
                          clock=clock)
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=clock() + 24 * HOUR, velocity_limit=3,
        velocity_window_secs=HOUR, created_at=clock()))
    store = IdempotencyStore(str(tmp_path / "idem.db"), clock=clock)

    def execute(request: PurchaseRequest) -> dict:
        if faults.fire_once(Fault.PROVIDER_TIMEOUT):
            raise ProviderTimeout(
                "order creation timed out; the order may or may not exist")
        order = provider.create_order(request.amount_paise, receipt=RECEIPT)
        if faults.fire_once(Fault.CRASH_AFTER_PROVIDER_CALL):
            # The money moved. This process dies before recording it.
            raise InjectedCrash(
                "process died after the provider call, before the ledger write")
        return order

    gateway = PurchaseGateway(engine, store, execute, audit=audit)
    reconciler = Reconciler(provider, store, audit=audit, policy=engine,
                            clock=clock)
    return {"audit": audit, "engine": engine, "store": store,
            "gateway": gateway, "reconciler": reconciler,
            "provider": provider, "faults": faults, "clock": clock}


def req(key="key-1", amount=15_000):
    return PurchaseRequest(AGENT, "SKU-COFFEE", amount, key)


def force_pending(stack, key="key-1"):
    """Drive a key into PENDING_VERIFICATION the way production does.

    Marking a record directly would skip the claim, and the store now refuses
    to mark a key it has never seen -- deliberately, since an unrecorded doubt
    is the one thing that state exists to prevent.
    """
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req(key=key))
    assert stack["store"].get(key, agent_id=AGENT)["status"] == \
        PENDING_VERIFICATION
    return key


# -- 1. the failure is reproducibly triggerable ---------------------------

def test_the_crash_fires_only_when_armed(stack):
    """Not a flaky accident: nothing fails unless a fault is armed."""
    outcome = stack["gateway"].submit(req())
    assert outcome.executed
    assert stack["faults"].fired_faults == []


def test_the_crash_is_deterministic_when_armed(stack):
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)

    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())

    assert stack["faults"].fired_faults == [Fault.CRASH_AFTER_PROVIDER_CALL]
    # The money moved even though the ledger doesn't say so.
    assert len(stack["provider"].orders_for_receipt(RECEIPT)) == 1
    assert stack["store"].get("key-1", agent_id=AGENT)["status"] == FAILED


def test_the_fault_is_one_shot_so_the_retry_can_succeed(stack):
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())

    assert not stack["faults"].is_armed(Fault.CRASH_AFTER_PROVIDER_CALL)


# -- 2. reconciliation detects the divergence -----------------------------

def test_reconciliation_detects_the_divergence(stack):
    """Provider executed it; the ledger never recorded it."""
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())

    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.DIVERGED_REPAIRED
    assert result.resolved
    assert "repaired" in result.reason


def test_the_repair_records_the_providers_truth(stack):
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())
    provider_order = stack["provider"].orders_for_receipt(RECEIPT)[0]

    stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    record = stack["store"].get("key-1", agent_id=AGENT)
    assert record["status"] == COMPLETED
    import json
    assert json.loads(record["response"])["id"] == provider_order["id"]


def test_after_repair_a_retry_replays_and_does_not_charge_again(stack):
    """The point of repairing: the ledger can now answer correctly."""
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())
    stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    outcome = stack["gateway"].submit(req())

    assert outcome.outcome is Outcome.REPLAYED
    assert len(stack["provider"].orders_for_receipt(RECEIPT)) == 1


# -- 3. no false positives on the happy path ------------------------------

def test_reconciliation_does_not_cry_wolf(stack):
    """A noisy detector destroys trust in the real signal."""
    stack["gateway"].submit(req())

    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.CONSISTENT
    assert not result.needs_human
    logged = {e.event_type for e in stack["audit"].all()}
    assert EventType.DIVERGENCE_DETECTED not in logged


def test_a_genuine_failure_is_not_reported_as_divergence(stack, provider):
    """The provider never executed it; that is not a divergence."""
    force_pending(stack, "key-ghost")
    stack["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)

    result = stack["reconciler"].reconcile("key-ghost", "rcpt_never_used",
                                           agent_id=AGENT)

    assert result.finding is Finding.CONFIRMED_NOT_EXECUTED
    assert result.resolved


# -- 4. a genuinely unknown outcome stays PENDING_VERIFICATION ------------

def test_a_timeout_is_recorded_as_pending_not_failed(stack):
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)

    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())

    record = stack["store"].get("key-1", agent_id=AGENT)
    assert record["status"] == PENDING_VERIFICATION, (
        "an unknown outcome was force-classified"
    )


def test_a_pending_record_blocks_retries_instead_of_double_charging(stack):
    """The Phase 2 gap, closed.

    Retrying an unknown outcome is exactly how a timeout becomes a second
    charge. The claim refuses instead.
    """
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())

    outcome = stack["gateway"].submit(req())

    assert outcome.outcome is Outcome.AWAITING_VERIFICATION
    assert "double charge" in outcome.result.reason
    assert len(stack["provider"].orders_for_receipt(RECEIPT)) == 0


def test_staleness_never_reclaims_a_pending_record(stack, clock):
    """Staleness rescues crashed claimants. It must NOT rescue this."""
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())

    clock.advance(10 * HOUR)  # far beyond any staleness timeout
    outcome = stack["gateway"].submit(req())

    assert outcome.outcome is Outcome.AWAITING_VERIFICATION
    assert stack["store"].get("key-1", agent_id=AGENT)["status"] == \
        PENDING_VERIFICATION


def test_a_pending_purchase_holds_its_velocity_slot(stack):
    """Otherwise an agent buys extra budget by inducing timeouts."""
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())

    assert stack["engine"].slots_used(AGENT, HOUR) == 1, (
        "the slot was released, so timeouts buy extra velocity budget"
    )


def test_reconciliation_releases_the_slot_when_nothing_happened(stack):
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())
    assert stack["engine"].slots_used(AGENT, HOUR) == 1
    stack["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)

    stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert stack["engine"].slots_used(AGENT, HOUR) == 0


def test_an_unreachable_provider_leaves_the_record_pending(stack):
    """We asked and got no answer. That is not permission to decide."""
    class UnreachableProvider:
        def orders_for_receipt(self, receipt):
            raise ProviderTimeout("lookup timed out")

    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())

    reconciler = Reconciler(UnreachableProvider(), stack["store"],
                            audit=stack["audit"], policy=stack["engine"])
    result = reconciler.reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.STILL_UNKNOWN
    assert not result.resolved
    assert stack["store"].get("key-1", agent_id=AGENT)["status"] == \
        PENDING_VERIFICATION
    assert stack["engine"].slots_used(AGENT, HOUR) == 1  # still held


# -- 5. ambiguous divergence is flagged, never auto-repaired --------------

def test_two_orders_for_one_receipt_needs_a_human(stack, provider):
    """An automatic repair would have to choose. Choosing is guessing."""
    force_pending(stack)
    # Two orders reached the provider for one receipt.
    provider.create_order(15_000, receipt=RECEIPT)
    provider.create_order(15_000, receipt=RECEIPT)

    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.NEEDS_HUMAN_REVIEW
    assert result.needs_human
    assert "guess" in result.reason
    # Deliberately NOT repaired.
    assert stack["store"].get("key-1", agent_id=AGENT)["status"] == \
        PENDING_VERIFICATION


def test_an_amount_mismatch_needs_a_human(stack, provider):
    force_pending(stack)
    provider.create_order(99_000, receipt=RECEIPT)

    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT,
                                           expected_amount_paise=15_000)

    assert result.finding is Finding.NEEDS_HUMAN_REVIEW
    assert "99000" in result.reason


def test_a_ledger_success_the_provider_never_saw_needs_a_human(stack):
    """Deleting a recorded success is not something to automate."""
    stack["gateway"].submit(req())
    stack["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)

    result = stack["reconciler"].reconcile("key-1", "rcpt_unknown",
                                           agent_id=AGENT)

    assert result.finding is Finding.NEEDS_HUMAN_REVIEW
    assert stack["store"].get("key-1", agent_id=AGENT)["status"] == COMPLETED


# -- 6. the audit log shows the whole sequence ----------------------------

def test_the_audit_log_tells_the_full_story(stack):
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())
    stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    types = [e.event_type for e in stack["audit"].all()]

    # The original attempt...
    assert EventType.PURCHASE_REQUESTED in types
    assert EventType.POLICY_APPROVED in types
    assert EventType.PAYMENT_ATTEMPTED in types
    assert EventType.PAYMENT_FAILED in types
    # ...the detection...
    assert EventType.DIVERGENCE_DETECTED in types
    # ...and the resolution.
    assert EventType.DIVERGENCE_RESOLVED in types
    assert types.index(EventType.DIVERGENCE_DETECTED) < \
        types.index(EventType.DIVERGENCE_RESOLVED)


def test_a_pending_timeout_is_logged_as_pending(stack):
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        stack["gateway"].submit(req())

    pending = stack["audit"].of_type(EventType.PAYMENT_PENDING_VERIFICATION)
    assert len(pending) == 1
    assert pending[0].details["velocity_slot"] == "held pending reconciliation"
    # It must NOT be logged as a definite failure.
    assert stack["audit"].count_of(EventType.PAYMENT_FAILED) == 0


def test_human_review_is_logged_as_detected_but_not_resolved(stack, provider):
    force_pending(stack)
    provider.create_order(15_000, receipt=RECEIPT)
    provider.create_order(15_000, receipt=RECEIPT)

    stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert stack["audit"].count_of(EventType.DIVERGENCE_DETECTED) == 1
    assert stack["audit"].count_of(EventType.DIVERGENCE_RESOLVED) == 0


# -- double-repair safety (the Phase 1 pattern, one level up) -------------

def test_reconciling_twice_repairs_once(stack):
    """A sweep running twice must not repair (or compensate) twice."""
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())

    first = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)
    second = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert first.finding is Finding.DIVERGED_REPAIRED
    assert second.finding is Finding.CONSISTENT, (
        "the second pass repaired again instead of recognising a settled record"
    )
    assert len(stack["provider"].orders_for_receipt(RECEIPT)) == 1
    assert stack["audit"].count_of(EventType.DIVERGENCE_RESOLVED) == 1


def test_a_sweep_finds_every_pending_record(stack):
    force_pending(stack, "k1")
    force_pending(stack, "k2")

    assert len(stack["store"].pending_verification()) == 2
    stack["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)
    results = stack["reconciler"].sweep(lambda scoped: "rcpt_never_used")

    assert len(results) == 2
    assert all(r.finding is Finding.CONFIRMED_NOT_EXECUTED for r in results)
    assert stack["store"].pending_verification() == []


def test_marking_an_unknown_key_pending_fails_loudly(stack):
    """A silent no-op here would leave a money action in doubt, unrecorded."""
    with pytest.raises(KeyError, match="no such idempotency record"):
        stack["store"].mark_pending_verification(
            "never-claimed", "unknown", agent_id=AGENT)


# -- the provider's read model lags, so absence needs time ----------------

def test_absence_is_not_evidence_inside_the_grace_window(stack):
    """Verified against the real API: Razorpay's order list does not show an
    order created seconds ago. Concluding "never happened" from that would
    clear a real purchase for a retry -- a double charge.
    """
    force_pending(stack)

    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.STILL_UNKNOWN
    assert "absence is not yet evidence" in result.reason
    assert stack["store"].get("key-1", agent_id=AGENT)["status"] == \
        PENDING_VERIFICATION
    # And the slot stays held while the answer is unknown.
    assert stack["engine"].slots_used(AGENT, HOUR) == 1


def test_absence_becomes_evidence_once_the_window_passes(stack):
    force_pending(stack)
    assert stack["reconciler"].reconcile(
        "key-1", RECEIPT, agent_id=AGENT).finding is Finding.STILL_UNKNOWN

    stack["clock"].advance(DEFAULT_NOT_FOUND_GRACE_SECONDS + 1)
    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.CONFIRMED_NOT_EXECUTED


def test_a_found_order_is_repaired_immediately_without_waiting(stack):
    """The grace window only gates ABSENCE. A positive finding is instant."""
    stack["faults"].arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    with pytest.raises(InjectedCrash):
        stack["gateway"].submit(req())

    result = stack["reconciler"].reconcile("key-1", RECEIPT, agent_id=AGENT)

    assert result.finding is Finding.DIVERGED_REPAIRED
