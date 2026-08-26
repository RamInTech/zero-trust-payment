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
