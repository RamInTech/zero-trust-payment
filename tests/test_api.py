"""Phase 5 — the HTTP surface.

Uses FastAPI's TestClient, so no server needs to be running. These tests check
that the guarantees survive the transport: the same denials, the same
exactly-once behaviour, reached over HTTP.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from zerotrust.api import create_app
from zerotrust.audit import AuditLog
from zerotrust.catalog import demo_catalog
from zerotrust.checkout import CheckoutService
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.intent import RuleBasedIntentParser
from zerotrust.mandate import Mandate, MandateStore
from zerotrust.policy import PolicyEngine

HOUR = 3600.0
AGENT = "agent_1"


class FakeClock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


@pytest.fixture
def env(tmp_path):
    clock = FakeClock()
    catalog = demo_catalog()
    audit = AuditLog(str(tmp_path / "audit.db"), clock=clock)
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db"), clock=clock),
                          clock=clock)
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({"SKU-COFFEE", "SKU-CAKE", "SKU-TEA"}),
        expires_at=clock() + 24 * HOUR, velocity_limit=3,
        velocity_window_secs=HOUR, created_at=clock()))

    calls = []
    gateway = PurchaseGateway(
        engine, IdempotencyStore(str(tmp_path / "idem.db"), clock=clock),
        lambda r: (calls.append(r), {"order_id": f"order_{len(calls)}"})[1],
        audit=audit)
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog),
                               audit=audit, clock=clock)
    client = TestClient(create_app(checkout))
    return {"client": client, "calls": calls, "catalog": catalog,
            "engine": engine, "clock": clock, "audit": audit}


def test_catalog_is_readable(env):
    body = env["client"].get("/catalog").json()
    skus = {item["sku"] for item in body["items"]}
    assert "SKU-COFFEE" in skus
    assert all("price_paise" in item for item in body["items"])


def test_structured_intent_then_confirm(env):
    client = env["client"]
    created = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"})
    assert created.status_code == 201
    pending = created.json()["awaiting_confirmation"]
    assert pending["displayed_amount_paise"] == 15_000
    assert len(env["calls"]) == 0  # nothing executed on display

    confirmed = client.post(f"/intents/{pending['request_id']}/confirm", json={})
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["approved"] and body["executed"]
    assert body["idempotency_outcome"] == "EXECUTED"
    assert len(env["calls"]) == 1


def test_natural_language_intent_returns_a_draft(env):
    created = env["client"].post(
        "/intents", json={"agent_id": AGENT, "text": "buy me filter coffee"})
    assert created.status_code == 201
    body = created.json()
    assert body["awaiting_confirmation"]["sku"] == "SKU-COFFEE"
    assert "no policy check has run yet" in body["note"]
    assert len(env["calls"]) == 0


def test_unknown_item_is_404(env):
    r = env["client"].post("/purchase-intents",
                           json={"agent_id": AGENT, "sku": "SKU-YACHT"})
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "ITEM_NOT_IN_CATALOG"
    assert len(env["calls"]) == 0


def test_ambiguous_text_is_422(env):
    r = env["client"].post("/intents",
                           json={"agent_id": AGENT, "text": "buy the cheaper one"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NEEDS_CLARIFICATION"


def test_the_response_carries_match_kind_so_the_client_can_tell_why(env):
    """This is the field the chat UI reads to decide whether "add it to the
    catalog" belongs on screen at all -- it must survive the trip over HTTP,
    not just exist on the Python exception."""
    ambiguous = env["client"].post(
        "/intents", json={"agent_id": AGENT, "text": "coffee or tea?"})
    assert ambiguous.json()["detail"]["match_kind"] == "ambiguous"

    no_match = env["client"].post(
        "/intents", json={"agent_id": AGENT, "text": "buy me a yacht"})
    assert no_match.json()["detail"]["match_kind"] == "no_match"


def test_decline_halts_everything(env):
    client = env["client"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"}
                          ).json()["awaiting_confirmation"]

    declined = client.post(f"/intents/{pending['request_id']}/decline")
    assert declined.status_code == 200
    assert declined.json()["status"] == "DECLINED"
    assert len(env["calls"]) == 0

    again = client.post(f"/intents/{pending['request_id']}/confirm", json={})
    assert again.status_code == 409
    assert len(env["calls"]) == 0


def test_double_tap_over_http_charges_once(env):
    client = env["client"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"}
                          ).json()["awaiting_confirmation"]
    rid = pending["request_id"]

    outcomes = [
        client.post(f"/intents/{rid}/confirm", json={}).json()["idempotency_outcome"]
        for _ in range(4)
    ]
    assert outcomes == ["EXECUTED", "REPLAYED", "REPLAYED", "REPLAYED"]
    assert len(env["calls"]) == 1


def test_tampered_amount_over_http_is_rejected(env):
    client = env["client"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"}
                          ).json()["awaiting_confirmation"]

    r = client.post(f"/intents/{pending['request_id']}/confirm",
                    json={"amount_paise": 1})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PRICE_MISMATCH"
    assert len(env["calls"]) == 0


def test_price_change_while_pending_is_rejected(env):
    client = env["client"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"}
                          ).json()["awaiting_confirmation"]
    env["catalog"].set_price("SKU-COFFEE", 30_000)

    r = client.post(f"/intents/{pending['request_id']}/confirm", json={})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "PRICE_MISMATCH"
    assert len(env["calls"]) == 0


def test_policy_denial_surfaces_its_rule_over_http(env):
    client = env["client"]
    # SKU-BEANS is in the catalog but not in this mandate's allowlist.
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-BEANS"}
                          ).json()["awaiting_confirmation"]

    body = client.post(f"/intents/{pending['request_id']}/confirm", json={}).json()
    assert body["approved"] is False
    assert body["rule"] in ("SKU_NOT_ALLOWED", "AMOUNT_EXCEEDS_CAP")
    assert body["reason"]
    assert len(env["calls"]) == 0


def test_velocity_limit_holds_over_http(env):
    client = env["client"]
    for i in range(3):
        p = client.post("/purchase-intents",
                        json={"agent_id": AGENT, "sku": "SKU-TEA"}
                        ).json()["awaiting_confirmation"]
        assert client.post(f"/intents/{p['request_id']}/confirm",
                           json={}).json()["approved"]

    p = client.post("/purchase-intents",
                    json={"agent_id": AGENT, "sku": "SKU-TEA"}
                    ).json()["awaiting_confirmation"]
    body = client.post(f"/intents/{p['request_id']}/confirm", json={}).json()

    assert body["approved"] is False
    assert body["rule"] == "VELOCITY_EXCEEDED"
    assert len(env["calls"]) == 3


def test_audit_endpoint_explains_a_request(env):
    client = env["client"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"}
                          ).json()["awaiting_confirmation"]
    rid = pending["request_id"]
    client.post(f"/intents/{rid}/confirm", json={})

    events = client.get(f"/audit/{rid}").json()["events"]
    types = [e["event_type"] for e in events]
    assert "USER_CONFIRMED" in types
    assert "POLICY_APPROVED" in types
    assert "PAYMENT_CAPTURED" in types
    # The actor column survives the HTTP boundary.
    confirmed = next(e for e in events if e["event_type"] == "USER_CONFIRMED")
    assert confirmed["actor"] == "HUMAN"
    approved = next(e for e in events if e["event_type"] == "POLICY_APPROVED")
    assert approved["actor"] == "POLICY_ENGINE"


def test_expired_pending_request_is_410(env):
    client = env["client"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-COFFEE"}
                          ).json()["awaiting_confirmation"]
    env["clock"].advance(1_000)

    r = client.post(f"/intents/{pending['request_id']}/confirm", json={})
    assert r.status_code == 410
    assert len(env["calls"]) == 0


def test_unknown_request_id_is_404(env):
    r = env["client"].post("/intents/req_nope/confirm", json={})
    assert r.status_code == 404
