"""Phase 2 — provider integration, offline.

These tests need no credentials: the network is stubbed with
httpx.MockTransport, which intercepts below the request-building code, so the
auth header, URL and JSON body under test are the real ones.

The live half of Phase 2's completion test lives in tests/test_razorpay_live.py.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from zerotrust.config import (
    DEFAULT_BASE_URL,
    MissingCredentialsError,
    RazorpayConfig,
)
from zerotrust.idempotency import IdempotencyStore, Outcome
from zerotrust.provider import (
    PaymentProvider,
    ProviderError,
    ProviderTimeout,
    RazorpayTestModeProvider,
    SimulatedProvider,
)

TEST_CONFIG = RazorpayConfig(key_id="rzp_test_fake123", key_secret="secret456")


def order_response(amount=50_000, order_id="order_TESTfake0001"):
    return {
        "id": order_id,
        "entity": "order",
        "amount": amount,
        "amount_paid": 0,
        "amount_due": amount,
        "currency": "INR",
        "receipt": "rcpt_test",
        "status": "created",
        "created_at": 1_700_000_000,
    }


def stub_provider(handler):
    return RazorpayTestModeProvider(
        TEST_CONFIG, transport=httpx.MockTransport(handler)
    )


# -- the request we actually send -----------------------------------------

def test_create_order_sends_a_correctly_formed_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=order_response())

    with stub_provider(handler) as provider:
        order = provider.create_order(50_000, "INR", receipt="rcpt_test")

    assert seen["method"] == "POST"
    assert seen["url"] == f"{DEFAULT_BASE_URL}/v1/orders"
    # Razorpay authenticates with HTTP Basic (key_id:key_secret).
    expected = base64.b64encode(b"rzp_test_fake123:secret456").decode()
    assert seen["auth"] == f"Basic {expected}"
    assert seen["body"] == {
        "amount": 50_000,
        "currency": "INR",
        "receipt": "rcpt_test",
    }
    assert order["id"] == "order_TESTfake0001"
    assert order["status"] == "created"


def test_receipt_is_generated_when_not_supplied():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=order_response())

    with stub_provider(handler) as provider:
        provider.create_order(1_000)

    assert seen["body"]["receipt"].startswith("rcpt_")


def test_amount_must_be_positive():
    with stub_provider(lambda r: httpx.Response(200, json=order_response())) as p:
        for bad in (0, -1):
            with pytest.raises(ValueError):
                p.create_order(bad)
        assert p.call_count == 0  # never reached the network


# -- how failures surface --------------------------------------------------

def test_api_error_surfaces_razorpay_description():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "BAD_REQUEST_ERROR",
                    "description": "amount must be atleast INR 1.00",
                }
            },
        )

    with stub_provider(handler) as provider:
        with pytest.raises(ProviderError) as exc:
            provider.create_order(50)

    assert "amount must be atleast INR 1.00" in str(exc.value)
    assert exc.value.status_code == 400


def test_timeout_raises_a_distinct_error_type():
    """A timeout means UNKNOWN, not failed -- it must be distinguishable."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with stub_provider(handler) as provider:
        with pytest.raises(ProviderTimeout) as exc:
            provider.create_order(50_000)

    assert "may or may not" in str(exc.value)
    # ProviderTimeout is a ProviderError, so generic handlers still catch it.
    assert isinstance(exc.value, ProviderError)


# -- capture is simulated, and says so -------------------------------------

@pytest.mark.parametrize(
    "provider",
    [SimulatedProvider(), stub_provider(lambda r: httpx.Response(200, json={}))],
    ids=["simulated", "razorpay"],
)
def test_capture_is_always_flagged_as_simulated(provider):
    result = provider.simulate_capture("order_abc", 50_000)
    assert result["simulated"] is True
    assert result["status"] == "captured"
    assert "browser" in result["simulation_reason"]


def test_provider_has_no_live_capture_method():
    """Guard against a future 'capture()' quietly appearing and being trusted."""
    for cls in (RazorpayTestModeProvider, SimulatedProvider):
        assert not hasattr(cls, "capture"), (
            f"{cls.__name__} grew a capture() method -- if capture became real, "
            f"update RAZORPAY.md Section 4 and this test deliberately"
        )


# -- credential handling ---------------------------------------------------

def test_from_env_rejects_a_live_key(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_dangerous")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    with pytest.raises(MissingCredentialsError) as exc:
        RazorpayConfig.from_env(load_dotenv_file=False)
    assert "test-mode" in str(exc.value)


def test_from_env_names_what_is_missing(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(MissingCredentialsError) as exc:
        RazorpayConfig.from_env(load_dotenv_file=False)
    assert "RAZORPAY_KEY_ID" in str(exc.value)
    assert ".env.example" in str(exc.value)


def test_from_env_accepts_a_test_key(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "shh")
    config = RazorpayConfig.from_env(load_dotenv_file=False)
    assert config.key_id == "rzp_test_abc"
    assert config.base_url == DEFAULT_BASE_URL


# -- both providers satisfy the same seam ----------------------------------

def test_both_providers_satisfy_the_protocol():
    assert isinstance(SimulatedProvider(), PaymentProvider)
    with stub_provider(lambda r: httpx.Response(200, json=order_response())) as p:
        assert isinstance(p, PaymentProvider)


# -- COMPLETION BULLET 3: the wrapper cannot tell the difference ------------

def _retry_sequence(store, provider, key, retries=4):
    """Same intent submitted `retries` times through the idempotency wrapper."""
    p = {"amount_paise": 50_000, "currency": "INR", "receipt": "rcpt_x"}
    return [
        store.execute(
            key,
            p,
            lambda: provider.create_order(
                p["amount_paise"], p["currency"], p["receipt"]
            ),
        )
        for _ in range(retries)
    ]


def test_wrapper_behaviour_is_identical_for_real_and_simulated(tmp_path):
    """Phase 1's wrapper is unchanged and provider-agnostic.

    The same retry sequence, run against a real-HTTP provider and an offline
    one, must produce the same outcomes and exactly one underlying call each.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=order_response(order_id="order_REAL01"))

    razorpay = stub_provider(handler)
    simulated = SimulatedProvider()

    real_store = IdempotencyStore(str(tmp_path / "real.db"))
    sim_store = IdempotencyStore(str(tmp_path / "sim.db"))

    real_results = _retry_sequence(real_store, razorpay, "key-real")
    sim_results = _retry_sequence(sim_store, simulated, "key-sim")
    razorpay.close()

    expected = [Outcome.EXECUTED] + [Outcome.REPLAYED] * 3
    assert [r.outcome for r in real_results] == expected
    assert [r.outcome for r in sim_results] == expected

    # Exactly one order created on each side, despite four submissions.
    assert calls["n"] == 1
    assert razorpay.call_count == 1
    assert simulated.call_count == 1

    # Every replay hands back the original order id, not a new one.
    for results in (real_results, sim_results):
        first_id = results[0].response["id"]
        assert all(r.response["id"] == first_id for r in results)


def test_conflicting_payload_never_reaches_the_provider(tmp_path):
    """A tampered retry must be rejected before any API call is made."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=order_response())

    store = IdempotencyStore(str(tmp_path / "idem.db"))
    with stub_provider(handler) as provider:
        original = {"amount_paise": 50_000, "currency": "INR", "receipt": "r"}
        tampered = {"amount_paise": 5_000_000, "currency": "INR", "receipt": "r"}

        first = store.execute(
            "key-1", original, lambda: provider.create_order(original["amount_paise"])
        )
        conflict = store.execute(
            "key-1", tampered, lambda: provider.create_order(tampered["amount_paise"])
        )

    assert first.outcome is Outcome.EXECUTED
    assert conflict.outcome is Outcome.CONFLICT
    assert calls["n"] == 1, "the tampered retry reached Razorpay -- it must not"


def test_a_failed_provider_call_leaves_the_key_retryable(tmp_path):
    """An API error must not permanently burn the idempotency key."""
    state = {"fail": True, "calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["fail"]:
            return httpx.Response(
                500, json={"error": {"description": "server error"}}
            )
        return httpx.Response(200, json=order_response())

    store = IdempotencyStore(str(tmp_path / "idem.db"))
    p = {"amount_paise": 50_000, "currency": "INR", "receipt": "r"}

    with stub_provider(handler) as provider:
        with pytest.raises(ProviderError):
            store.execute("key-1", p, lambda: provider.create_order(50_000))

        state["fail"] = False
        retry = store.execute("key-1", p, lambda: provider.create_order(50_000))

    assert retry.outcome is Outcome.EXECUTED
    assert retry.attempts == 2
    assert state["calls"] == 2
