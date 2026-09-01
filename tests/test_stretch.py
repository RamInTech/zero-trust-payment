"""Stretch goals from RAZORPAY.md Section 8.

Covers the periodic reconciliation sweep, the explain-this-decision endpoint,
the audit-entry narrators, and multi-mandate isolation under concurrency. The
denial cool-down is tested alongside the policy engine it belongs to, in
tests/test_policy.py.
"""

from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from zerotrust.api import create_app
from zerotrust.audit import Actor, AuditEntry, AuditLog, EventType
from zerotrust.catalog import demo_catalog
from zerotrust.checkout import CheckoutService
from zerotrust.explain import UnknownRequest, explain
from zerotrust.faults import Fault, FaultInjector
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import COMPLETED, IdempotencyStore
from zerotrust.intent import RuleBasedIntentParser
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.narrate import ClaudeNarrator, ExplanationWriter, TemplateNarrator
from zerotrust.policy import PolicyEngine, PurchaseRequest, Rule
from zerotrust.provider import ProviderTimeout, SimulatedProvider
from zerotrust.reconcile import (
    Finding,
    ReconciliationScheduler,
    Reconciler,
)

HOUR = 3600.0
AGENT = "agent_1"
RECEIPT = "rcpt_stretch"


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
def stack(tmp_path, clock):
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "audit.db"), clock=clock)
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db"), clock=clock),
                          clock=clock)
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE", "SKU-CAKE", "SKU-TEA"}),
        expires_at=clock() + 24 * HOUR, velocity_limit=3,
        velocity_window_secs=HOUR, created_at=clock()))
    store = IdempotencyStore(str(tmp_path / "idem.db"), clock=clock)
    provider = SimulatedProvider()
    faults = FaultInjector()
    calls = []

    def execute(request):
        if faults.fire_once(Fault.PROVIDER_TIMEOUT):
            raise ProviderTimeout("timed out; outcome unknown")
        calls.append(request)
        return provider.create_order(request.amount_paise, receipt=RECEIPT)

    gateway = PurchaseGateway(engine, store, execute, audit=audit)
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog),
                               audit=audit, clock=clock)
    return {"audit": audit, "engine": engine, "store": store, "calls": calls,
            "gateway": gateway, "checkout": checkout, "catalog": catalog,
            "provider": provider, "faults": faults, "clock": clock,
            "client": TestClient(create_app(checkout))}


def buy(stack, sku="SKU-COFFEE"):
    pending = stack["checkout"].propose(AGENT, sku)
    outcome = stack["checkout"].confirm(pending.request_id)
    return pending.request_id, outcome


# ========================================================================
# 1. Periodic reconciliation sweep
# ========================================================================

def scheduler_for(stack, interval=0.02):
    reconciler = Reconciler(stack["provider"], stack["store"],
                            audit=stack["audit"], policy=stack["engine"],
                            clock=stack["clock"])
    return ReconciliationScheduler(reconciler, lambda scoped: RECEIPT,
                                   interval_seconds=interval,
                                   clock=stack["clock"])


def test_the_scheduler_runs_repeatedly_and_stops_cleanly(stack):
    scheduler = scheduler_for(stack)
    with scheduler:
        time.sleep(0.25)
    assert scheduler.status()["cycles"] >= 2
    assert scheduler.running is False


def test_the_scheduler_resolves_a_pending_record_without_a_human(stack):
    """The gap Phase 7 left open: nothing called sweep()."""
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        buy(stack)
    assert len(stack["store"].pending_verification()) == 1

    # The provider did execute it after all; the ledger never found out.
    stack["provider"].create_order(15_000, receipt=RECEIPT)

    scheduler_for(stack).run_once()

    assert stack["store"].pending_verification() == []
    assert stack["audit"].count_of(EventType.DIVERGENCE_RESOLVED) == 1


def test_a_cycle_that_raises_does_not_kill_the_loop(stack):
    """A scheduler that dies on the first provider blip is worse than none."""
    class ExplodingReconciler:
        def sweep(self, receipt_for):
            raise RuntimeError("provider unreachable")

    scheduler = ReconciliationScheduler(ExplodingReconciler(), lambda k: RECEIPT,
                                        interval_seconds=0.02)
    with scheduler:
        time.sleep(0.2)

    status = scheduler.status()
    assert status["cycles"] >= 2, "the loop stopped at the first error"
    assert status["errors"] == status["cycles"]
    assert "provider unreachable" in status["last_cycle"]["error"]


def test_sweeps_do_not_overlap(stack):
    """A slow provider must not cause sweeps to stack up behind each other."""
    concurrent = []
    active = threading.Semaphore(1)

    class SlowReconciler:
        def sweep(self, receipt_for):
            acquired = active.acquire(blocking=False)
            concurrent.append(acquired)
            time.sleep(0.06)
            if acquired:
                active.release()
            return []

    scheduler = ReconciliationScheduler(SlowReconciler(), lambda k: RECEIPT,
                                        interval_seconds=0.01)
    with scheduler:
        time.sleep(0.35)

    assert all(concurrent), "two sweeps ran at once"


def test_status_reports_what_was_resolved(stack):
    stack["faults"].arm(Fault.PROVIDER_TIMEOUT)
    with pytest.raises(ProviderTimeout):
        buy(stack)
    stack["provider"].create_order(15_000, receipt=RECEIPT)

    scheduler = scheduler_for(stack)
    scheduler.run_once()

    status = scheduler.status()
    assert status["cycles"] == 1
    assert status["last_cycle"]["records_seen"] == 1
    assert status["records_resolved"][Finding.DIVERGED_REPAIRED.value] == 1
    assert status["errors"] == 0


def test_an_idle_sweep_finds_nothing_and_says_so(stack):
    buy(stack)  # a clean purchase leaves nothing pending
    cycle = scheduler_for(stack).run_once()
    assert cycle.records_seen == 0
    assert cycle.error is None


# ========================================================================
# 2. Explain-this-decision
# ========================================================================

def test_an_approval_explains_itself(stack):
    request_id, _ = buy(stack)

    result = explain(stack["audit"], request_id)

    assert result.what["agent_id"] == AGENT
    assert result.what["sku"] == "SKU-COFFEE"
    assert result.what["outcome"] == "COMPLETED"
    assert result.what["money_moved"] is True
    assert result.why["decision"] == "APPROVED"
    assert result.why["decided_by"] == "POLICY_ENGINE"
    assert result.why["human_confirmed"] is True
    assert result.evidence


def test_a_denial_explains_which_rule_and_by_how_much(stack):
    stack["catalog"].set_price("SKU-CAKE", 90_000)
    request_id, _ = buy(stack, "SKU-CAKE")

    result = explain(stack["audit"], request_id)

    assert result.why["decision"] == "DENIED"
    assert result.why["rule"] == Rule.AMOUNT_EXCEEDS_CAP.value
    assert result.why["figures"]["cap_paise"] == 50_000
    assert result.why["figures"]["requested_paise"] == 90_000
    assert result.what["money_moved"] is False


def test_the_explanation_records_that_a_human_confirmed_a_denied_request(stack):
    """The project's central claim, made legible in one field."""
    for i in range(3):
        buy(stack, "SKU-TEA")
    request_id, outcome = buy(stack, "SKU-TEA")

    result = explain(stack["audit"], request_id)

    assert outcome.rule is Rule.VELOCITY_EXCEEDED
    assert result.why["decision"] == "DENIED"
    assert result.why["human_confirmed"] is True
    assert result.why["decided_by"] == "POLICY_ENGINE"


def test_a_declined_request_is_explained_as_declined(stack):
    pending = stack["checkout"].propose(AGENT, "SKU-COFFEE")
    stack["checkout"].decline(pending.request_id)

    result = explain(stack["audit"], pending.request_id)

    assert result.what["outcome"] == "DECLINED"
    assert result.why["decision"] == "UNDECIDED"
    assert "never reached the policy engine" in result.why["reason"]


def test_evidence_preserves_order_and_actors(stack):
    request_id, _ = buy(stack)
    evidence = explain(stack["audit"], request_id).evidence

    ids = [e["event_id"] for e in evidence]
    assert ids == sorted(ids)
    actors = {e["event_type"]: e["actor"] for e in evidence}
    assert actors["PURCHASE_REQUESTED"] == "AGENT"
    assert actors["USER_CONFIRMED"] == "HUMAN"
    assert actors["POLICY_APPROVED"] == "POLICY_ENGINE"


def test_an_unknown_request_raises(stack):
    with pytest.raises(UnknownRequest):
        explain(stack["audit"], "req_nope")


def test_the_endpoint_serves_the_explanation(stack):
    request_id, _ = buy(stack)
    body = stack["client"].get(f"/explain/{request_id}").json()

    assert body["request_id"] == request_id
    assert body["why"]["decision"] == "APPROVED"
    assert body["what"]["outcome"] == "COMPLETED"
    assert len(body["evidence"]) >= 5


def test_the_endpoint_404s_for_an_unknown_request(stack):
    response = stack["client"].get("/explain/req_nope")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "UNKNOWN_REQUEST"


def test_explaining_writes_nothing(stack):
    """A read-only view must not disturb the record it reads."""
    request_id, _ = buy(stack)
    before = len(stack["audit"].all())

    for _ in range(3):
        stack["client"].get(f"/explain/{request_id}")

    assert len(stack["audit"].all()) == before


# ========================================================================
# 3. Narrators — explanation, never authority
# ========================================================================

def entry(**kwargs) -> AuditEntry:
    base = dict(event_id=1, event_type=EventType.POLICY_DENIED,
                actor=Actor.POLICY_ENGINE, occurred_at=0.0,
                rule="AMOUNT_EXCEEDS_CAP", reason="amount 90000 exceeds cap 50000")
    base.update(kwargs)
    return AuditEntry(**base)


def test_both_narrators_satisfy_the_protocol():
    class Stub:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            class _B:
                type = "text"
                text = "A purchase was refused for exceeding its limit."

            class _R:
                content = [_B()]

            return _R()

    assert isinstance(TemplateNarrator(), ExplanationWriter)
    assert isinstance(ClaudeNarrator(client=Stub()), ExplanationWriter)


def test_the_template_narrator_keeps_the_rule_name(stack):
    text = TemplateNarrator().narrate(entry())
    assert "AMOUNT_EXCEEDS_CAP" in text
    assert "refused" in text


def test_a_narrator_holds_no_writable_component():
    """Structural: there is no path through which it could alter a decision.

    The same argument that keeps ParsedIntent unable to state a price -- the
    capability is absent, not merely unused.
    """
    for narrator in (TemplateNarrator(), ClaudeNarrator(client=object())):
        for attribute in vars(narrator).values():
            for forbidden in ("execute", "record", "resolve_verified",
                              "confirm_slot", "issue", "evaluate"):
                assert not hasattr(attribute, forbidden), (
                    f"a narrator holds something with .{forbidden}()"
                )


def test_a_narrator_returns_a_string_and_nothing_else():
    result = TemplateNarrator().narrate(entry())
    assert isinstance(result, str)


def test_a_failing_claude_narrator_falls_back_rather_than_breaking(stack):
    """An explanation must not disappear because a model call failed."""
    class Broken:
        def __init__(self):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("api down")

    text = ClaudeNarrator(client=Broken()).narrate(entry())
    assert "AMOUNT_EXCEEDS_CAP" in text


def test_narratives_appear_on_the_explanation_when_asked(stack):
    request_id, _ = buy(stack)

    plain = explain(stack["audit"], request_id)
    narrated = explain(stack["audit"], request_id, narrator=TemplateNarrator())

    assert "narrative" not in plain.evidence[0]
    assert narrated.evidence[0]["narrative"]


# ========================================================================
# 4. Multiple concurrent mandates
# ========================================================================

@pytest.fixture
def many_agents(tmp_path, clock):
    engine = PolicyEngine(MandateStore(str(tmp_path / "multi.db"), clock=clock),
                          clock=clock)
    specs = {
        "agent_small": dict(max_amount_paise=10_000,
                            allowed_skus=frozenset({"SKU-TEA"}),
                            velocity_limit=1),
        "agent_medium": dict(max_amount_paise=50_000,
                             allowed_skus=frozenset({"SKU-TEA", "SKU-COFFEE"}),
                             velocity_limit=3),
        "agent_large": dict(max_amount_paise=1_000_000,
                            allowed_skus=frozenset({"SKU-TEA", "SKU-COFFEE",
                                                    "SKU-BEANS"}),
                            velocity_limit=10),
    }
    for agent_id, spec in specs.items():
        engine.mandates.issue(Mandate(
            agent_id=agent_id, expires_at=clock() + 24 * HOUR,
            velocity_window_secs=HOUR, created_at=clock(), **spec))
    return engine, specs


def test_each_agent_is_held_to_its_own_cap(many_agents):
    engine, _ = many_agents
    amount = 40_000
    assert engine.evaluate(
        PurchaseRequest("agent_small", "SKU-TEA", amount, "s")).rule \
        is Rule.AMOUNT_EXCEEDS_CAP
    assert engine.evaluate(
        PurchaseRequest("agent_medium", "SKU-TEA", amount, "m")).approved
    assert engine.evaluate(
        PurchaseRequest("agent_large", "SKU-TEA", amount, "l")).approved


def test_each_agent_is_held_to_its_own_allowlist(many_agents):
    engine, _ = many_agents
    assert engine.evaluate(
        PurchaseRequest("agent_medium", "SKU-BEANS", 5_000, "m")).rule \
        is Rule.SKU_NOT_ALLOWED
    assert engine.evaluate(
        PurchaseRequest("agent_large", "SKU-BEANS", 5_000, "l")).approved


def test_velocity_budgets_are_independent(many_agents):
    engine, _ = many_agents
    assert engine.evaluate(
        PurchaseRequest("agent_small", "SKU-TEA", 5_000, "s1")).approved
    assert engine.evaluate(
        PurchaseRequest("agent_small", "SKU-TEA", 5_000, "s2")).rule \
        is Rule.VELOCITY_EXCEEDED

    # The exhausted agent must not have touched anybody else's budget.
    for i in range(3):
        assert engine.evaluate(
            PurchaseRequest("agent_medium", "SKU-TEA", 5_000, f"m{i}")).approved


def test_revoking_one_mandate_leaves_the_others_working(many_agents):
    engine, _ = many_agents
    mandate = engine.mandates.active_for_agent("agent_medium")
    engine.mandates.revoke(mandate.mandate_id)

    assert engine.evaluate(
        PurchaseRequest("agent_medium", "SKU-TEA", 5_000, "m")).rule \
        is Rule.NO_ACTIVE_MANDATE
    assert engine.evaluate(
        PurchaseRequest("agent_large", "SKU-TEA", 5_000, "l")).approved


@pytest.mark.parametrize("run", range(6))
def test_concurrent_agents_do_not_contaminate_each_other(many_agents, run):
    """All three spending at once, each held to its own limits."""
    engine, _ = many_agents
    attempts = 6
    agents = ["agent_small", "agent_medium", "agent_large"]
    barrier = threading.Barrier(len(agents) * attempts)
    results = {a: [] for a in agents}
    lock = threading.Lock()

    def worker(agent_id, i):
        barrier.wait()
        decision = engine.evaluate(
            PurchaseRequest(agent_id, "SKU-TEA", 5_000, f"{agent_id}-{i}"))
        with lock:
            results[agent_id].append(decision)

    threads = [threading.Thread(target=worker, args=(a, i))
               for a in agents for i in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    approved = {a: sum(1 for d in results[a] if d.approved) for a in agents}
    assert approved["agent_small"] == 1
    assert approved["agent_medium"] == 3
    assert approved["agent_large"] == 6  # limit is 10, only 6 attempted
    for agent_id in agents:
        for decision in results[agent_id]:
            if decision.denied:
                assert decision.rule is Rule.VELOCITY_EXCEEDED
