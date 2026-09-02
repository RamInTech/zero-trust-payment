"""End-to-end encrypted chat: zerotrust/e2e.py, its wiring into CheckoutService,
and the /intents + /e2e/public-key HTTP surface.

The property under test throughout: the audit log stores ciphertext for an
encrypted request, never the customer's words -- while the system still
functions exactly as before, because the plaintext is recovered in memory
for parsing.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from zerotrust.api import create_app
from zerotrust.audit import AuditLog
from zerotrust.catalog import demo_catalog
from zerotrust.checkout import CheckoutError, CheckoutService
from zerotrust.e2e import (
    DecryptionFailed, SealedText, ServerIdentity, generate_keypair, open_sealed, seal,
)
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.intent import RuleBasedIntentParser
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine

HOUR = 3600.0
AGENT = "agent_1"


# -- the crypto primitive, in isolation ------------------------------------

def test_a_sealed_message_round_trips():
    sender_sk, sender_pk = generate_keypair()
    recipient_sk, recipient_pk = generate_keypair()

    sealed = seal("buy me 2 mugs", sender_sk, recipient_pk)
    assert open_sealed(sealed, recipient_sk) == "buy me 2 mugs"


def test_the_ciphertext_does_not_contain_the_plaintext():
    sender_sk, _ = generate_keypair()
    recipient_sk, recipient_pk = generate_keypair()

    sealed = seal("a very specific secret sentence", sender_sk, recipient_pk)
    raw = base64.b64decode(sealed.ciphertext_b64)
    assert b"secret" not in raw
    assert b"specific" not in raw


def test_the_wrong_private_key_cannot_open_it():
    sender_sk, _ = generate_keypair()
    recipient_sk, recipient_pk = generate_keypair()
    someone_elses_sk, _ = generate_keypair()

    sealed = seal("only for the recipient", sender_sk, recipient_pk)
    with pytest.raises(DecryptionFailed):
        open_sealed(sealed, someone_elses_sk)


def test_tampering_with_the_ciphertext_is_detected():
    sender_sk, _ = generate_keypair()
    recipient_sk, recipient_pk = generate_keypair()

    sealed = seal("do not modify this", sender_sk, recipient_pk)
    raw = bytearray(base64.b64decode(sealed.ciphertext_b64))
    raw[-1] ^= 0xFF  # flip a bit near the authentication tag
    tampered = SealedText(
        ciphertext_b64=base64.b64encode(bytes(raw)).decode(),
        sender_public_key_b64=sealed.sender_public_key_b64,
    )
    with pytest.raises(DecryptionFailed):
        open_sealed(tampered, recipient_sk)


def test_server_identity_exposes_only_the_public_key_by_default():
    identity = ServerIdentity()
    assert isinstance(identity.public_key_b64, str)
    assert len(base64.b64decode(identity.public_key_b64)) == 32  # X25519 key size


def test_server_identity_can_open_what_was_sealed_to_it():
    identity = ServerIdentity()
    sender_sk, _ = generate_keypair()
    sealed = seal("hello server", sender_sk, identity.public_key_b64)
    assert identity.open(sealed) == "hello server"


def test_server_identity_is_stable_when_given_a_fixed_private_key():
    identity_a = ServerIdentity()
    identity_b = ServerIdentity(identity_a.private_key_b64)
    assert identity_a.public_key_b64 == identity_b.public_key_b64


# -- wired into CheckoutService --------------------------------------------

@pytest.fixture
def checkout_env(tmp_path):
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=10_000_000_000.0, velocity_limit=5, velocity_window_secs=HOUR))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    gateway = PurchaseGateway(
        engine, store, lambda r: {"order_id": "order_1"}, audit=audit)
    identity = ServerIdentity()
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog), audit=audit,
                               server_identity=identity)
    return {"checkout": checkout, "audit": audit, "identity": identity}


def test_a_sealed_purchase_request_is_parsed_correctly(checkout_env):
    checkout, identity = checkout_env["checkout"], checkout_env["identity"]
    sender_sk, _ = generate_keypair()
    sealed = seal("buy me filter coffee", sender_sk, identity.public_key_b64)

    pending = checkout.propose_from_text(AGENT, sealed=sealed)
    assert pending.sku == "SKU-COFFEE"


def test_the_audit_log_stores_ciphertext_not_the_customers_words(checkout_env):
    checkout, audit, identity = (
        checkout_env["checkout"], checkout_env["audit"], checkout_env["identity"])
    sender_sk, _ = generate_keypair()
    secret_words = "buy filter coffee, and this exact sentence must not be readable"
    sealed = seal(secret_words, sender_sk, identity.public_key_b64)

    checkout.propose_from_text(AGENT, sealed=sealed)

    from zerotrust.audit import EventType
    entries = [e for e in audit.all() if e.event_type == EventType.INTENT_PARSED]
    assert len(entries) == 1
    details = entries[0].details
    assert "raw_text" not in details
    assert "raw_text_sealed" in details
    stored_ciphertext = details["raw_text_sealed"]["ciphertext_b64"]
    assert "coffee" not in base64.b64decode(stored_ciphertext).decode("latin-1")
    assert "readable" not in base64.b64decode(stored_ciphertext).decode("latin-1")


def test_plaintext_calls_still_log_plaintext_unchanged(checkout_env):
    """No regression for callers that never opt into encryption."""
    checkout, audit = checkout_env["checkout"], checkout_env["audit"]
    checkout.propose_from_text(AGENT, "buy filter coffee")

    from zerotrust.audit import EventType
    entries = [e for e in audit.all() if e.event_type == EventType.INTENT_PARSED]
    assert entries[0].details["raw_text"] == "buy filter coffee"
    assert "raw_text_sealed" not in entries[0].details


def test_sealed_text_without_a_configured_identity_is_refused(checkout_env):
    checkout = checkout_env["checkout"]
    checkout.server_identity = None
    sender_sk, recipient_pk = generate_keypair()
    sealed = seal("buy coffee", sender_sk, recipient_pk)

    with pytest.raises(CheckoutError) as exc:
        checkout.propose_from_text(AGENT, sealed=sealed)
    assert exc.value.code == "NO_E2E"


def test_a_message_sealed_to_the_wrong_key_is_refused_not_guessed(checkout_env):
    checkout = checkout_env["checkout"]
    sender_sk, _ = generate_keypair()
    _, some_other_public_key = generate_keypair()
    sealed = seal("buy coffee", sender_sk, some_other_public_key)  # wrong recipient

    with pytest.raises(CheckoutError) as exc:
        checkout.propose_from_text(AGENT, sealed=sealed)
    assert exc.value.code == "DECRYPTION_FAILED"


# -- the HTTP surface --------------------------------------------------------

@pytest.fixture
def api_env(tmp_path):
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE"}),
        expires_at=10_000_000_000.0, velocity_limit=5, velocity_window_secs=HOUR))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    gateway = PurchaseGateway(
        engine, store, lambda r: {"order_id": "order_1"}, audit=audit)
    identity = ServerIdentity()
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog), audit=audit,
                               server_identity=identity)
    client = TestClient(create_app(checkout))
    return {"client": client, "identity": identity, "audit": audit}


def test_the_public_key_endpoint_serves_a_real_x25519_key(api_env):
    res = api_env["client"].get("/e2e/public-key")
    assert res.status_code == 200
    body = res.json()
    assert body["public_key_b64"] == api_env["identity"].public_key_b64
    assert len(base64.b64decode(body["public_key_b64"])) == 32


def test_the_public_key_endpoint_404s_without_a_configured_identity(tmp_path):
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    gateway = PurchaseGateway(engine, store, lambda r: {}, audit=audit)
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog), audit=audit)
    client = TestClient(create_app(checkout))
    assert client.get("/e2e/public-key").status_code == 501


def test_posting_a_sealed_intent_over_http_produces_a_draft(api_env):
    client, identity = api_env["client"], api_env["identity"]
    key_res = client.get("/e2e/public-key")
    sender_sk, _ = generate_keypair()
    sealed = seal("buy filter coffee", sender_sk, key_res.json()["public_key_b64"])

    res = client.post("/intents", json={
        "agent_id": AGENT,
        "sealed": {"ciphertext_b64": sealed.ciphertext_b64,
                   "sender_public_key_b64": sealed.sender_public_key_b64},
    })
    assert res.status_code == 201
    assert res.json()["awaiting_confirmation"]["sku"] == "SKU-COFFEE"


def test_the_stored_audit_entry_for_a_sealed_request_is_unreadable_over_http(api_env):
    client, audit = api_env["client"], api_env["audit"]
    key_res = client.get("/e2e/public-key")
    sender_sk, _ = generate_keypair()
    secret = "buy filter coffee -- nobody with just the database should read this"
    sealed = seal(secret, sender_sk, key_res.json()["public_key_b64"])

    res = client.post("/intents", json={
        "agent_id": AGENT,
        "sealed": {"ciphertext_b64": sealed.ciphertext_b64,
                   "sender_public_key_b64": sealed.sender_public_key_b64},
    })
    assert res.status_code == 201
    assert res.json()["awaiting_confirmation"]["sku"] == "SKU-COFFEE"

    from zerotrust.audit import EventType
    entries = [e for e in audit.all() if e.event_type == EventType.INTENT_PARSED]
    assert len(entries) == 1
    assert "raw_text" not in entries[0].details
    ciphertext = entries[0].details["raw_text_sealed"]["ciphertext_b64"]
    assert "nobody" not in base64.b64decode(ciphertext).decode("latin-1")
