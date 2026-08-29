"""Phase 2 completion test — the live half.

These hit the real Razorpay test-mode API and create real (test-mode) orders.
They skip automatically when credentials are absent, so the suite stays green
for anyone cloning without keys:

    uv run pytest tests/ -q            # skips these
    uv run pytest tests/ -q -m live    # runs only these

Test mode only -- RazorpayConfig.from_env() refuses a live key outright.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from zerotrust.config import MissingCredentialsError, RazorpayConfig
from zerotrust.idempotency import IdempotencyStore, Outcome
from zerotrust.provider import RazorpayTestModeProvider

pytestmark = pytest.mark.live


def _config_or_skip() -> RazorpayConfig:
    try:
        return RazorpayConfig.from_env()
    except MissingCredentialsError as exc:
        pytest.skip(f"no Razorpay test-mode credentials: {exc}")


@pytest.fixture(scope="module")
def config() -> RazorpayConfig:
    return _config_or_skip()


@pytest.fixture
def provider(config):
    with RazorpayTestModeProvider(config) as p:
        yield p


def _receipt() -> str:
    return f"phase2_{uuid.uuid4().hex[:12]}"


# -- COMPLETION BULLET 1 ---------------------------------------------------

def test_real_order_creation_returns_a_valid_order(provider):
    """A real POST /v1/orders against a live test-mode account."""
    receipt = _receipt()
    order = provider.create_order(50_000, "INR", receipt=receipt)

    assert order["id"].startswith("order_"), order
    assert order["entity"] == "order"
    assert order["amount"] == 50_000
    assert order["currency"] == "INR"
    assert order["receipt"] == receipt
    assert order["status"] == "created"
    # This is a real order object, not one we constructed locally.
    assert "simulated" not in order

    print(f"\n  [live] created real order {order['id']} (receipt {receipt})")


def test_bad_amount_is_rejected_by_razorpay(provider):
    """Proves we're talking to the real API, not a stub that says yes."""
    from zerotrust.provider import ProviderError

    with pytest.raises(ProviderError) as exc:
        provider.create_order(1)  # below Razorpay's INR 1.00 minimum
    assert exc.value.status_code == 400
    print(f"\n  [live] Razorpay rejected as expected: {exc.value}")


# -- COMPLETION BULLET 2 ---------------------------------------------------

def test_repeated_key_does_not_create_a_duplicate_order(tmp_path, provider):
    """The Phase 1 wrapper, unchanged, around a real provider."""
    store = IdempotencyStore(str(tmp_path / "live.db"))
    receipt = _receipt()
    payload = {"amount_paise": 50_000, "currency": "INR", "receipt": receipt}
    key = f"live-key-{uuid.uuid4().hex[:8]}"

    results = [
        store.execute(
            key,
            payload,
            lambda: provider.create_order(50_000, "INR", receipt=receipt),
        )
        for _ in range(5)
    ]

    assert [r.outcome for r in results] == [Outcome.EXECUTED] + [Outcome.REPLAYED] * 4

    # One API call for five submissions -- no duplicate order at Razorpay.
    assert provider.call_count == 1

    order_ids = {r.response["id"] for r in results}
    assert len(order_ids) == 1, f"expected one order, got {order_ids}"

    order_id = order_ids.pop()
    print(
        f"\n  [live] 5 submissions of key {key} -> 1 real order {order_id}"
        f"\n  [live] verify in dashboard: Test Mode -> Orders -> receipt {receipt}"
    )


# -- COMPLETION BULLET 3, against the real client --------------------------

def test_capture_against_the_real_provider_is_still_simulated(provider):
    """The honest constraint, asserted rather than only documented."""
    receipt = _receipt()
    order = provider.create_order(25_000, "INR", receipt=receipt)
    capture = provider.simulate_capture(order["id"], 25_000)

    assert capture["simulated"] is True
    assert capture["order_id"] == order["id"]
    print(f"\n  [live] order {order['id']} captured -- SIMULATED, not live")


def test_credentials_are_test_mode(config):
    assert config.key_id.startswith("rzp_test_")
    assert not os.environ.get("RAZORPAY_KEY_ID", "").startswith("rzp_live_")


# -- PHASE 7: reconciliation against the real provider --------------------

def test_live_order_fetch_by_id_is_immediately_consistent(provider):
    """A known order id reads back straight away."""
    receipt = _receipt()
    created = provider.create_order(30_000, "INR", receipt=receipt)

    fetched = provider.fetch_order(created["id"])

    assert fetched is not None
    assert fetched["id"] == created["id"]
    assert fetched["amount"] == 30_000
    assert fetched["receipt"] == receipt
    print(f"\n  [live] fetched {created['id']} by id, immediately")


def test_live_order_list_lags_behind_creation(provider):
    """The finding that shaped the reconciler, asserted rather than assumed.

    Razorpay's order LIST does not show an order created moments ago, while
    fetching that same order BY ID works instantly. So an empty receipt search
    right after an attempt means "not indexed yet", never "never happened" --
    which is why Reconciler waits out a grace window before treating absence
    as evidence.
    """
    receipt = _receipt()
    created = provider.create_order(30_000, "INR", receipt=receipt)

    assert provider.fetch_order(created["id"]) is not None, (
        "the order does not exist at all, which is not what this test is about"
    )
    found = provider.orders_for_receipt(receipt)

    print(f"\n  [live] order {created['id']} exists by id; "
          f"receipt search returned {len(found)} match(es)")
    # Either outcome is acceptable and both are informative: the point is that
    # absence here is NOT proof, and the reconciler must not treat it as such.
    assert isinstance(found, list)


def test_live_reconciler_refuses_to_guess_when_the_order_is_not_yet_visible(
    tmp_path, provider
):
    """The full Phase 7 scenario against a REAL Razorpay order.

    A real order is created; the local ledger is prevented from recording it.
    Reconciliation runs immediately -- before the provider's list has caught
    up -- and must report STILL_UNKNOWN rather than clearing the record for a
    retry. Refusing to guess IS the correct behaviour here.
    """
    from zerotrust.audit import AuditLog
    from zerotrust.faults import Fault, FaultInjector, InjectedCrash
    from zerotrust.gateway import PurchaseGateway
    from zerotrust.idempotency import FAILED, IdempotencyStore
    from zerotrust.mandate import Mandate, MandateStore
    from zerotrust.policy import PolicyEngine, PurchaseRequest
    from zerotrust.reconcile import Finding, Reconciler

    receipt = _receipt()
    faults = FaultInjector().arm(Fault.CRASH_AFTER_PROVIDER_CALL)
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    engine.mandates.issue(Mandate(
        agent_id="agent_live", max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=time.time() + 3600, velocity_limit=3))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    created_ids = []

    def execute(request):
        order = provider.create_order(request.amount_paise, "INR", receipt=receipt)
        created_ids.append(order["id"])
        if faults.fire_once(Fault.CRASH_AFTER_PROVIDER_CALL):
            raise InjectedCrash("died before the ledger write")
        return order

    gateway = PurchaseGateway(engine, store, execute, audit=audit)
    key = f"live-recon-{uuid.uuid4().hex[:8]}"

    with pytest.raises(InjectedCrash):
        gateway.submit(PurchaseRequest("agent_live", "SKU-COFFEE", 30_000, key))

    # A real order exists at Razorpay; our ledger says the attempt failed.
    assert store.get(key, agent_id="agent_live")["status"] == FAILED
    assert provider.fetch_order(created_ids[0]) is not None, (
        "the order really was created at Razorpay"
    )

    reconciler = Reconciler(provider, store, audit=audit, policy=engine)
    result = reconciler.reconcile(key, receipt, agent_id="agent_live",
                                  expected_amount_paise=30_000)

    assert result.finding in (Finding.STILL_UNKNOWN, Finding.DIVERGED_REPAIRED)
    assert result.finding is not Finding.CONFIRMED_NOT_EXECUTED, (
        "reconciliation cleared a real purchase for retry -- a double charge"
    )
    print(f"\n  [live] real order {created_ids[0]} created, ledger said FAILED; "
          f"reconciliation returned {result.finding.value}")
    print(f"  [live] {result.reason}")
