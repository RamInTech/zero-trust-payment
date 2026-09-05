"""Inbound webhooks: signature verification, and the limits of what one buys.

The property under test throughout: a webhook may INFORM the system, and may
never AUTHORISE anything. A forged delivery is refused; a genuine one causes a
reconciliation and no ledger write at all.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from zerotrust.audit import Actor, AuditLog, EventType
from zerotrust.catalog import demo_catalog
from zerotrust.checkout import CheckoutService
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine
from zerotrust.api import create_app
from zerotrust.webhook import (
    DEFAULT_MAX_AGE_SECONDS, Rejection, SIGNATURE_HEADER, WebhookReceiver,
    compute_signature, verify_signature,
)

SECRET = "whsec_test_do_not_reuse"
HOUR = 3600.0
AGENT = "agent_1"


def body_for(receipt: str = "ui_abc123", event: str = "order.paid",
             created_at: float | None = None) -> bytes:
    """A Razorpay-shaped payload, serialised once and signed as-is."""
    payload = {
        "entity": "event",
        "event": event,
        "created_at": int(created_at if created_at is not None else time.time()),
        "payload": {
            "order": {
                "entity": {
                    "id": "order_TEST123",
                    "receipt": receipt,
                    "amount": 15000,
                    "status": "paid",
                }
            }
        },
    }
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def receiver(tmp_path):
    audit = AuditLog(str(tmp_path / "audit.db"))
    reconciled: list[str] = []
    rx = WebhookReceiver(SECRET, audit=audit, on_verified=reconciled.append)
    return rx, audit, reconciled


# -- the primitive ---------------------------------------------------------

def test_a_genuine_signature_verifies():
    body = body_for()
    assert verify_signature(body, compute_signature(body, SECRET), SECRET)


def test_a_tampered_body_does_not_verify():
    """The attack this exists to stop: same signature, edited payload."""
    original = body_for(receipt="ui_abc123")
    signature = compute_signature(original, SECRET)
    tampered = original.replace(b'"amount": 15000', b'"amount": 1')
    assert tampered != original
    assert verify_signature(tampered, signature, SECRET) is False


def test_a_signature_from_the_wrong_secret_does_not_verify():
    body = body_for()
    assert verify_signature(body, compute_signature(body, "not_the_secret"), SECRET) is False


def test_a_missing_secret_refuses_rather_than_bypasses():
    """The dangerous default. An unset secret must close the door, not open it."""
    body = body_for()
    assert verify_signature(body, compute_signature(body, SECRET), None) is False
    assert verify_signature(body, None, SECRET) is False


def test_the_signature_covers_the_exact_bytes_not_the_parsed_json():
    """Re-serialising changes the bytes, so it must change the verdict.

    This is the classic webhook bug: verifying against `json.dumps(parsed)`
    instead of the raw body. Key order and separators differ, so it fails on
    genuine traffic — and any implementation loose enough to paper over that
    is loose enough to accept a forgery.
    """
    body = body_for()
    signature = compute_signature(body, SECRET)
    reserialised = json.dumps(json.loads(body), sort_keys=True).encode("utf-8")
    assert reserialised != body
    assert verify_signature(reserialised, signature, SECRET) is False


# -- the receiver ----------------------------------------------------------

def test_a_verified_delivery_requests_a_reconciliation(receiver):
    rx, audit, reconciled = receiver
    body = body_for(receipt="ui_xyz")
    result = rx.receive(body, compute_signature(body, SECRET))

    assert result.accepted is True
    assert result.reconcile_requested is True
    assert result.receipt == "ui_xyz"
    assert reconciled == ["ui_xyz"], "the receipt should have reached the reconciler"

    logged = audit.of_type(EventType.WEBHOOK_RECEIVED)
    assert len(logged) == 1
    assert logged[0].actor is Actor.PROVIDER
    assert logged[0].details["effect"] == "reconciliation requested; no ledger write"


def test_a_forged_delivery_is_refused_and_causes_no_work(receiver):
    rx, audit, reconciled = receiver
    body = body_for()
    result = rx.receive(body, compute_signature(body, "wrong_secret"))

    assert result.accepted is False
    assert result.rejection is Rejection.BAD_SIGNATURE
    assert result.reconcile_requested is False
    assert reconciled == [], "a forged webhook must not trigger any work"


def test_a_rejected_delivery_is_logged_as_unverified_not_as_the_provider(receiver):
    """Filing a forgery under PROVIDER would be a false claim in the log."""
    rx, audit, _ = receiver
    body = body_for()
    rx.receive(body, "0" * 64)

    entries = audit.of_type(EventType.WEBHOOK_REJECTED)
    assert len(entries) == 1
    assert entries[0].actor is Actor.UNVERIFIED
    assert entries[0].actor is not Actor.PROVIDER
    assert entries[0].rule == Rejection.BAD_SIGNATURE.value
    assert audit.of_type(EventType.WEBHOOK_RECEIVED) == []


def test_a_missing_signature_header_is_refused(receiver):
    rx, _, reconciled = receiver
    result = rx.receive(body_for(), None)
    assert result.accepted is False
    assert result.rejection is Rejection.MISSING_SIGNATURE
    assert reconciled == []


def test_an_unconfigured_receiver_refuses_everything(tmp_path):
    audit = AuditLog(str(tmp_path / "a.db"))
    reconciled: list[str] = []
    rx = WebhookReceiver(None, audit=audit, on_verified=reconciled.append)
    body = body_for()

    assert rx.is_configured is False
    result = rx.receive(body, compute_signature(body, SECRET))
    assert result.accepted is False
    assert result.rejection is Rejection.NO_SECRET
    assert reconciled == []


def test_a_retried_delivery_does_not_reconcile_twice(receiver):
    """Razorpay retries. The receiver must tolerate that without duplicating work."""
    rx, _, reconciled = receiver
    body = body_for(receipt="ui_retry")
    signature = compute_signature(body, SECRET)

    first = rx.receive(body, signature)
    second = rx.receive(body, signature)

    assert first.accepted is True and first.reconcile_requested is True
    assert second.accepted is True
    assert second.rejection is Rejection.ALREADY_SEEN
    assert second.reconcile_requested is False
    assert reconciled == ["ui_retry"], "a retry must not re-trigger reconciliation"


def test_a_stale_replayed_delivery_is_refused(receiver):
    """A correctly-signed body captured off the wire and replayed much later."""
    rx, _, reconciled = receiver
    old = body_for(created_at=time.time() - DEFAULT_MAX_AGE_SECONDS - 60)
    result = rx.receive(old, compute_signature(old, SECRET))

    assert result.accepted is False
    assert result.rejection is Rejection.STALE
    assert reconciled == []


def test_a_verified_delivery_with_no_receipt_reconciles_nothing(receiver):
    """Nothing to check against, so nothing is claimed."""
    rx, _, reconciled = receiver
    body = json.dumps({"event": "payment.failed", "payload": {}}).encode()
    result = rx.receive(body, compute_signature(body, SECRET))

    assert result.accepted is True
    assert result.reconcile_requested is False
    assert reconciled == []


def test_the_payloads_own_numbers_are_never_believed(receiver):
    """A verified webhook hands over a receipt, not an amount or a status.

    This is the invariant that keeps a leaked signing secret from becoming a
    ledger write: whoever holds the secret can cause a reconciliation, and
    reconciliation trusts only what the provider's API says when asked.
    """
    rx, _, reconciled = receiver
    body = body_for(receipt="ui_only")
    rx.receive(body, compute_signature(body, SECRET))

    assert reconciled == ["ui_only"]
    assert all(isinstance(x, str) for x in reconciled)


# -- over HTTP -------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=time.time() + HOUR, velocity_limit=5,
        velocity_window_secs=HOUR))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    gateway = PurchaseGateway(engine, store, lambda r: {"id": "order_1"}, audit=audit)
    checkout = CheckoutService(catalog, gateway, audit=audit)
    reconciled: list[str] = []
    rx = WebhookReceiver(SECRET, audit=audit, on_verified=reconciled.append)
    return TestClient(create_app(checkout, webhooks=rx)), reconciled, audit


def test_a_verified_post_returns_200(client):
    api, reconciled, _ = client
    body = body_for(receipt="ui_http")
    res = api.post("/webhooks/razorpay", content=body,
                   headers={SIGNATURE_HEADER: compute_signature(body, SECRET)})

    assert res.status_code == 200
    assert res.json()["accepted"] is True
    assert reconciled == ["ui_http"]


def test_a_tampered_post_returns_401_and_changes_nothing(client):
    """The headline claim, end to end over HTTP."""
    api, reconciled, audit = client
    original = body_for(receipt="ui_http")
    signature = compute_signature(original, SECRET)
    tampered = original.replace(b'"receipt": "ui_http"', b'"receipt": "ui_ATTACK"')

    res = api.post("/webhooks/razorpay", content=tampered,
                   headers={SIGNATURE_HEADER: signature})

    assert res.status_code == 401
    assert res.json()["rejection"] == "BAD_SIGNATURE"
    assert reconciled == []
    assert audit.of_type(EventType.WEBHOOK_RECEIVED) == []


def test_an_unsigned_post_returns_401(client):
    api, reconciled, _ = client
    res = api.post("/webhooks/razorpay", content=body_for())
    assert res.status_code == 401
    assert res.json()["rejection"] == "MISSING_SIGNATURE"
    assert reconciled == []


def test_the_route_is_absent_when_no_receiver_is_configured(tmp_path):
    """An app built without a receiver must not silently accept deliveries."""
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "a.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "p.db")))
    store = IdempotencyStore(str(tmp_path / "i.db"))
    gateway = PurchaseGateway(engine, store, lambda r: {"id": "o"}, audit=audit)
    checkout = CheckoutService(catalog, gateway, audit=audit)

    api = TestClient(create_app(checkout))
    body = body_for()
    res = api.post("/webhooks/razorpay", content=body,
                   headers={SIGNATURE_HEADER: compute_signature(body, SECRET)})
    assert res.status_code == 501


def test_the_demo_app_forwards_the_receiver_to_the_production_api(tmp_path):
    """The wiring, not just the receiver.

    `create_demo_app` mounts the production app and must pass the receiver
    through. When it did not, every endpoint test still passed — they build
    `create_app` directly — while the running server answered 501 to every
    delivery. Only a test that goes through the demo app catches that.
    """
    from zerotrust.demo import create_demo_app

    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "a.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "p.db")))
    store = IdempotencyStore(str(tmp_path / "i.db"))
    gateway = PurchaseGateway(engine, store, lambda r: {"id": "o"}, audit=audit)
    checkout = CheckoutService(catalog, gateway, audit=audit)
    reconciled: list[str] = []
    rx = WebhookReceiver(SECRET, audit=audit, on_verified=reconciled.append)

    app = create_demo_app(checkout, engine, audit, catalog, webhooks=rx)
    client = TestClient(app)

    body = body_for(receipt="ui_wired")
    res = client.post("/api/webhooks/razorpay", content=body,
                      headers={SIGNATURE_HEADER: compute_signature(body, SECRET)})

    assert res.status_code == 200, "the receiver did not reach the mounted API"
    assert reconciled == ["ui_wired"]

    forged = client.post("/api/webhooks/razorpay", content=body,
                         headers={SIGNATURE_HEADER: "0" * 64})
    assert forged.status_code == 401
