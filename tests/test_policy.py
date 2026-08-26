"""Phase 3 completion test — Policy Engine (Mandate Enforcement).

Each group maps to one bullet of Phase 3's completion test in RAZORPAY.md.

The recurring assertion is `len(calls) == 0`: a denied request must never reach
the execution layer. Asserting on the provider call count -- rather than reading
the code and trusting the ordering -- is what makes that claim checkable.
"""

from __future__ import annotations

import threading

import pytest

from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore, Outcome
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine, PurchaseRequest, Rule

HOUR = 3600.0
# The mandate must outlive the velocity window, or a sliding-window test
# trips over expiry instead of measuring what it means to measure.
MANDATE_TTL = 24 * HOUR


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
def engine(tmp_path, clock):
    store = MandateStore(str(tmp_path / "policy.db"), clock=clock)
    return PolicyEngine(store, clock=clock)


@pytest.fixture
def mandate(engine, clock):
    return engine.mandates.issue(
        Mandate(
            agent_id="agent_1",
            max_amount_paise=50_000,          # Rs. 500 per transaction
            allowed_skus=frozenset({"SKU-COFFEE", "SKU-CAKE"}),
            expires_at=clock() + MANDATE_TTL,
            velocity_limit=3,
            velocity_window_secs=HOUR,
            created_at=clock(),
        )
    )


def req(**overrides) -> PurchaseRequest:
    base = dict(
        agent_id="agent_1",
        sku="SKU-COFFEE",
        amount_paise=10_000,
        idempotency_key="key-1",
        currency="INR",
    )
    base.update(overrides)
    return PurchaseRequest(**base)


@pytest.fixture
def gateway(engine, tmp_path):
    """Gateway with a recording executor, so denials are provably inert."""
    calls: list[PurchaseRequest] = []
    store = IdempotencyStore(str(tmp_path / "idem.db"))

    def execute(request: PurchaseRequest) -> dict:
        calls.append(request)
        return {"order_id": f"order_{len(calls)}", "amount": request.amount_paise}

    gw = PurchaseGateway(engine, store, execute)
    gw.calls = calls  # test-only handle
    return gw


# -- 1. a compliant request is approved and reaches execution --------------

def test_compliant_request_is_approved_and_executes(gateway, mandate):
    outcome = gateway.submit(req())

    assert outcome.approved
    assert outcome.rule is None
    assert outcome.outcome is Outcome.EXECUTED
    assert outcome.response["order_id"] == "order_1"
    assert len(gateway.calls) == 1


def test_approval_cites_the_governing_mandate(engine, mandate):
    decision = engine.evaluate(req())
    assert decision.approved
    assert decision.mandate_id == mandate.mandate_id


# -- 2. over the per-transaction cap --------------------------------------

def test_amount_over_cap_is_denied_citing_the_cap(gateway, mandate):
    outcome = gateway.submit(req(amount_paise=50_001))

    assert outcome.denied
    assert outcome.rule is Rule.AMOUNT_EXCEEDS_CAP
    assert "50000" in outcome.reason  # the cap that was violated
    assert outcome.decision.details["cap_paise"] == 50_000
    assert outcome.decision.details["requested_paise"] == 50_001
    assert len(gateway.calls) == 0, "a denied request reached the executor"


def test_amount_exactly_at_the_cap_is_allowed(gateway, mandate):
    outcome = gateway.submit(req(amount_paise=50_000))
    assert outcome.approved, "the cap is inclusive; 'max' means at most"
    assert len(gateway.calls) == 1


# -- 3. disallowed SKU -----------------------------------------------------

def test_disallowed_sku_is_denied_and_names_the_item(gateway, mandate):
    outcome = gateway.submit(req(sku="SKU-YACHT"))

    assert outcome.denied
    assert outcome.rule is Rule.SKU_NOT_ALLOWED
    assert "SKU-YACHT" in outcome.reason
    assert outcome.decision.details["requested_sku"] == "SKU-YACHT"
    assert outcome.decision.details["allowed_skus"] == ["SKU-CAKE", "SKU-COFFEE"]
    assert len(gateway.calls) == 0


def test_allowlist_fails_closed_for_unknown_items(gateway, mandate):
    """Anything not named is denied -- the safe default for money."""
    for sku in ("", "sku-coffee", "SKU-COFFEE ", "SKU-UNKNOWN"):
        outcome = gateway.submit(req(sku=sku, idempotency_key=f"k-{sku}"))
        assert outcome.denied, f"{sku!r} was allowed"
    assert len(gateway.calls) == 0


# -- 4. expired mandate ----------------------------------------------------

def test_expired_mandate_is_denied(gateway, mandate, clock):
    clock.advance(MANDATE_TTL + 1)
    outcome = gateway.submit(req())

    assert outcome.denied
    assert outcome.rule is Rule.MANDATE_EXPIRED
    assert "expired" in outcome.reason
    assert len(gateway.calls) == 0


def test_request_just_before_expiry_still_works(gateway, mandate, clock):
    clock.advance(MANDATE_TTL - 1)
    assert gateway.submit(req()).approved


def test_expiry_denial_is_specific_not_no_mandate(engine, mandate, clock):
    """An expired mandate must say 'expired', not 'no mandate' -- specificity."""
    clock.advance(MANDATE_TTL + 1)
    decision = engine.evaluate(req())
    assert decision.rule is Rule.MANDATE_EXPIRED
    assert decision.rule is not Rule.NO_ACTIVE_MANDATE


# -- 5. velocity limit -----------------------------------------------------

def test_fourth_purchase_is_denied_by_velocity(gateway, mandate):
    for i in range(3):
        assert gateway.submit(req(idempotency_key=f"key-{i}")).approved

    fourth = gateway.submit(req(idempotency_key="key-3"))

    assert fourth.denied
    assert fourth.rule is Rule.VELOCITY_EXCEEDED
    assert "3 of 3" in fourth.reason
    assert fourth.decision.details["limit"] == 3
    assert len(gateway.calls) == 3, "the 4th purchase reached the executor"


def test_velocity_window_slides(gateway, mandate, clock):
    """A sliding window frees budget gradually, not all at once on the hour."""
    for i in range(3):
        assert gateway.submit(req(idempotency_key=f"key-{i}")).approved
    assert gateway.submit(req(idempotency_key="key-3")).denied

    # Move past the first purchase's window: exactly one slot frees up.
    clock.advance(HOUR + 1)
    assert gateway.submit(req(idempotency_key="key-4")).approved
    assert len(gateway.calls) == 4


def test_fixed_window_boundary_exploit_does_not_work(gateway, mandate, clock):
    """The reason we chose sliding over fixed.

    Under a fixed window an agent fires its cap at the end of one window and
    the cap again just after the reset, doubling its spend. A sliding window
    still counts the earlier purchases.
    """
    for i in range(3):
        assert gateway.submit(req(idempotency_key=f"early-{i}")).approved

    clock.advance(60)  # a fixed hourly window might have reset by now
    for i in range(3):
        outcome = gateway.submit(req(idempotency_key=f"late-{i}"))
        assert outcome.denied
        assert outcome.rule is Rule.VELOCITY_EXCEEDED
    assert len(gateway.calls) == 3


def test_retries_do_not_consume_extra_velocity_budget(gateway, mandate):
    """A retry is the same intent, so it must reuse its slot, not take a new one."""
    for _ in range(5):
        gateway.submit(req(idempotency_key="key-same"))

    # One purchase's worth of budget used, despite five submissions.
    assert len(gateway.calls) == 1
    for i in range(2):
        assert gateway.submit(req(idempotency_key=f"other-{i}")).approved
    assert gateway.submit(req(idempotency_key="one-too-many")).denied
    assert len(gateway.calls) == 3


def test_a_failed_execution_releases_its_velocity_slot(engine, tmp_path, mandate):
    """A failure shouldn't quietly cost the agent budget."""
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        raise RuntimeError("provider down")

    gw = PurchaseGateway(engine, store, flaky)
    with pytest.raises(RuntimeError):
        gw.submit(req(idempotency_key="key-fail"))

    assert engine.slots_used("agent_1", HOUR) == 0, "failed attempt kept a slot"


# -- 6. denials never reach the execution layer ---------------------------

@pytest.mark.parametrize(
    "overrides,expected_rule",
    [
        ({"amount_paise": 999_999}, Rule.AMOUNT_EXCEEDS_CAP),
        ({"sku": "SKU-BANNED"}, Rule.SKU_NOT_ALLOWED),
        ({"currency": "USD"}, Rule.CURRENCY_MISMATCH),
        ({"amount_paise": 0}, Rule.MALFORMED_REQUEST),
        ({"amount_paise": -5_000}, Rule.MALFORMED_REQUEST),
        ({"agent_id": "agent_unknown"}, Rule.NO_ACTIVE_MANDATE),
    ],
)
def test_every_denial_is_inert_and_specific(gateway, mandate, overrides, expected_rule):
    outcome = gateway.submit(req(**overrides))

    assert outcome.denied
    assert outcome.rule is expected_rule
    assert outcome.reason, "a denial must carry a specific reason"
    assert outcome.result is None, "a denied request created an idempotency record"
    assert len(gateway.calls) == 0


def test_denial_leaves_no_idempotency_record(gateway, mandate):
    """A denied request must not burn its key -- a corrected retry can reuse it."""
    denied = gateway.submit(req(sku="SKU-BANNED", idempotency_key="key-x"))
    assert denied.denied
    assert gateway.store.get("key-x", agent_id="agent_1") is None

    corrected = gateway.submit(req(sku="SKU-COFFEE", idempotency_key="key-x"))
    assert corrected.approved
    assert len(gateway.calls) == 1


# -- edge cases from RAZORPAY.md section 6 --------------------------------

def test_negative_and_zero_amounts_are_malformed_not_policy_denials(engine, mandate):
    """Rejected as bad input, before being weighed against the cap."""
    for bad in (0, -1, -50_000):
        decision = engine.evaluate(req(amount_paise=bad))
        assert decision.rule is Rule.MALFORMED_REQUEST
        assert decision.rule is not Rule.AMOUNT_EXCEEDS_CAP


def test_currency_mismatch_is_rejected_not_coerced(engine, mandate):
    decision = engine.evaluate(req(currency="USD"))
    assert decision.rule is Rule.CURRENCY_MISMATCH
    assert decision.details["mandate_currency"] == "INR"


def test_revoked_mandate_denies_further_purchases(gateway, engine, mandate):
    assert gateway.submit(req(idempotency_key="before")).approved
    engine.mandates.revoke(mandate.mandate_id)

    outcome = gateway.submit(req(idempotency_key="after"))
    assert outcome.denied
    assert outcome.rule is Rule.NO_ACTIVE_MANDATE
    assert len(gateway.calls) == 1


def test_mandates_are_isolated_between_agents(engine, clock, tmp_path):
    engine.mandates.issue(
        Mandate(
            agent_id="agent_rich",
            max_amount_paise=10_000_000,
            allowed_skus=frozenset({"SKU-YACHT"}),
            expires_at=clock() + HOUR,
            velocity_limit=10,
            created_at=clock(),
        )
    )
    engine.mandates.issue(
        Mandate(
            agent_id="agent_poor",
            max_amount_paise=100,
            allowed_skus=frozenset({"SKU-COFFEE"}),
            expires_at=clock() + HOUR,
            velocity_limit=1,
            created_at=clock(),
        )
    )

    rich = engine.evaluate(
        PurchaseRequest("agent_rich", "SKU-YACHT", 5_000_000, "k1")
    )
    poor = engine.evaluate(
        PurchaseRequest("agent_poor", "SKU-YACHT", 5_000_000, "k2")
    )
    assert rich.approved
    assert poor.denied, "one agent's mandate leaked to another"


# -- concurrency: a burst must not overshoot the cap ----------------------

@pytest.mark.parametrize("run", range(10))
def test_concurrent_burst_cannot_exceed_the_velocity_limit(tmp_path, run):
    """Read-then-act would let N threads all see 'budget available'.

    The slot is claimed inside one BEGIN IMMEDIATE transaction, so SQLite
    serialises the claimants and exactly `velocity_limit` win.
    """
    clock = FakeClock()
    mandate_store = MandateStore(str(tmp_path / f"p{run}.db"), clock=clock)
    engine = PolicyEngine(mandate_store, clock=clock)
    mandate_store.issue(
        Mandate(
            agent_id="agent_burst",
            max_amount_paise=50_000,
            allowed_skus=frozenset({"SKU-COFFEE"}),
            expires_at=clock() + HOUR,
            velocity_limit=3,
            created_at=clock(),
        )
    )

    threads_n = 20
    barrier = threading.Barrier(threads_n)
    decisions = []
    lock = threading.Lock()

    def worker(i):
        barrier.wait()
        d = engine.evaluate(
            PurchaseRequest("agent_burst", "SKU-COFFEE", 10_000, f"burst-{i}")
        )
        with lock:
            decisions.append(d)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    approved = [d for d in decisions if d.approved]
    assert len(approved) == 3, f"velocity cap overshot: {len(approved)} approved"
    for d in decisions:
        if d.denied:
            assert d.rule is Rule.VELOCITY_EXCEEDED
    assert engine.slots_used("agent_burst", HOUR) == 3
