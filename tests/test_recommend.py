"""Upsell suggestions are proposals, held to the same boundary as any request.

The load-bearing test here is
`test_a_suggested_item_over_the_cap_is_still_denied`. An upsell path that could
be confirmed without facing the policy engine would be a back door around the
mandate, and it would be an easy one to build by accident -- the suggestion
already knows the SKU and the price, so "just buy it" is one convenience method
away.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from zerotrust.api import create_app
from zerotrust.audit import AuditLog, EventType
from zerotrust.catalog import Catalog, CatalogItem, demo_catalog
from zerotrust.checkout import CheckoutService
from zerotrust.gateway import PurchaseGateway
from zerotrust.idempotency import IdempotencyStore
from zerotrust.intent import RuleBasedIntentParser
from zerotrust.mandate import ANY_SKU, Mandate, MandateStore
from zerotrust.policy import PolicyEngine
from zerotrust.recommend import (
    Recommender, StaticRecommender, Suggestion,
)

HOUR = 3600.0
AGENT = "agent_1"


@pytest.fixture
def catalog():
    return demo_catalog()


@pytest.fixture
def env(tmp_path, catalog):
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=50_000,
        allowed_skus=frozenset({ANY_SKU}),
        expires_at=time.time() + 24 * HOUR, velocity_limit=20,
        velocity_window_secs=HOUR))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    calls = []

    def execute(request):
        calls.append(request)
        return {"order_id": f"order_{len(calls)}"}

    gateway = PurchaseGateway(engine, store, execute, audit=audit)
    checkout = CheckoutService(catalog, gateway,
                               parser=RuleBasedIntentParser(catalog), audit=audit)
    client = TestClient(create_app(checkout, recommender=StaticRecommender(catalog)))
    return {"client": client, "calls": calls, "audit": audit, "catalog": catalog}


# -- the recommender itself -------------------------------------------------

def test_it_satisfies_the_protocol(catalog):
    assert isinstance(StaticRecommender(catalog), Recommender)


def test_a_suggestion_carries_no_price_or_approval_field():
    """Structural, exactly as with ParsedIntent.

    A suggestion that could state its own price would let the upsell path
    bypass confirm-time re-validation; one that could state an approval would
    bypass the policy engine outright.
    """
    fields = set(Suggestion.__dataclass_fields__)
    for forbidden in ("price_paise", "amount_paise", "price", "approved",
                      "authorised", "authorized", "pre_approved", "skip_policy"):
        assert forbidden not in fields


def test_it_suggests_a_complement(catalog):
    suggestions = StaticRecommender(catalog).suggest("SKU-COFFEE")
    assert suggestions
    assert suggestions[0].sku != "SKU-COFFEE"
    assert catalog.has(suggestions[0].sku)


def test_it_never_suggests_the_item_just_bought(catalog):
    pairings = {"SKU-COFFEE": [("SKU-COFFEE", "itself")]}
    assert StaticRecommender(catalog, pairings).suggest("SKU-COFFEE") == []


def test_it_drops_a_pairing_that_left_the_catalog(catalog):
    pairings = {"SKU-COFFEE": [("SKU-GONE", "no longer stocked")]}
    assert StaticRecommender(catalog, pairings).suggest("SKU-COFFEE") == []


def test_it_drops_an_out_of_stock_pairing():
    catalog = Catalog([
        CatalogItem("SKU-A", "A", 1_000),
        CatalogItem("SKU-B", "B", 1_000, available=False),
    ])
    pairings = {"SKU-A": [("SKU-B", "paired")]}
    assert StaticRecommender(catalog, pairings).suggest("SKU-A") == []


def test_an_unknown_sku_suggests_nothing(catalog):
    assert StaticRecommender(catalog).suggest("SKU-NOT-A-THING") == []


def test_the_limit_is_respected(catalog):
    assert len(StaticRecommender(catalog).suggest("SKU-COFFEE", limit=1)) == 1


# -- over HTTP --------------------------------------------------------------

def test_the_endpoint_returns_suggestions(env):
    body = env["client"].get("/recommendations/SKU-COFFEE").json()
    assert body["prompted_by"] == "SKU-COFFEE"
    assert body["suggestions"]
    first = body["suggestions"][0]
    assert first["sku"] and first["name"] and first["reason"]


def test_the_price_shown_comes_from_the_catalog(env):
    body = env["client"].get("/recommendations/SKU-COFFEE").json()
    first = body["suggestions"][0]
    assert first["price_paise"] == env["catalog"].get(first["sku"]).price_paise


def test_an_unknown_sku_is_a_404(env):
    assert env["client"].get("/recommendations/SKU-NOPE").status_code == 404


def test_offering_a_suggestion_executes_nothing(env):
    """Being shown an upsell must not move money."""
    env["client"].get("/recommendations/SKU-COFFEE")
    assert env["calls"] == []


def test_every_offer_is_logged(env):
    """Attach rate needs the denominator, and an unauditable upsell is worse."""
    env["client"].get("/recommendations/SKU-COFFEE")
    offered = [e for e in env["audit"].all()
               if e.event_type == EventType.SUGGESTION_OFFERED]
    assert len(offered) == 1
    assert offered[0].actor.value == "AGENT"     # proposes; never authorises
    assert offered[0].details["prompted_by_sku"] == "SKU-COFFEE"


def test_a_server_without_a_recommender_says_so(tmp_path, catalog):
    audit = AuditLog(str(tmp_path / "a.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "p.db")))
    store = IdempotencyStore(str(tmp_path / "i.db"))
    gateway = PurchaseGateway(engine, store, lambda r: {}, audit=audit)
    checkout = CheckoutService(catalog, gateway, audit=audit)
    client = TestClient(create_app(checkout))
    assert client.get("/recommendations/SKU-COFFEE").status_code == 501


# -- the boundary the upsell must not cross ---------------------------------

def test_a_suggested_item_is_bought_through_the_normal_gated_path(env):
    """Accepting an upsell is an ordinary purchase, not a shortcut."""
    client = env["client"]
    suggested = client.get("/recommendations/SKU-COFFEE").json()["suggestions"][0]

    created = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": suggested["sku"]})
    assert created.status_code == 201
    pending = created.json()["awaiting_confirmation"]

    # It is still only a draft: nothing has executed on the strength of a
    # suggestion the merchant's own recommender made.
    assert env["calls"] == []

    body = client.post(f"/intents/{pending['request_id']}/confirm", json={}).json()
    assert body["approved"] and body["executed"]


def test_a_suggested_item_over_the_cap_is_still_denied(env):
    """The whole reason this feature is allowed to exist.

    SKU-PHONE suggests a charger; the phone itself is far over the Rs.500 cap.
    Here the suggestion is deliberately confirmed by a human, and the mandate
    refuses it anyway -- an upsell gets no more authority than the customer's
    own request would have.
    """
    client = env["client"]
    created = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": "SKU-PHONE"})
    pending = created.json()["awaiting_confirmation"]

    body = client.post(f"/intents/{pending['request_id']}/confirm", json={}).json()
    assert not body["approved"]
    assert body["rule"] == "AMOUNT_EXCEEDS_CAP"
    assert env["calls"] == []


def test_suggestions_are_not_pre_filtered_by_the_mandate(tmp_path, catalog):
    """Deliberate: the recommender proposes, the policy engine decides.

    Filtering here would put the mandate in two places, and the recommender's
    copy would be the one no adversarial test attacks. So an unaffordable
    complement is still offered -- and still refused downstream.
    """
    audit = AuditLog(str(tmp_path / "audit.db"))
    engine = PolicyEngine(MandateStore(str(tmp_path / "policy.db")))
    engine.mandates.issue(Mandate(
        agent_id=AGENT, max_amount_paise=100,       # Rs.1 -- affords nothing
        allowed_skus=frozenset({ANY_SKU}),
        expires_at=time.time() + HOUR, velocity_limit=5,
        velocity_window_secs=HOUR))
    store = IdempotencyStore(str(tmp_path / "idem.db"))
    gateway = PurchaseGateway(engine, store, lambda r: {"order_id": "o"}, audit=audit)
    checkout = CheckoutService(catalog, gateway, audit=audit)
    client = TestClient(create_app(checkout, recommender=StaticRecommender(catalog)))

    body = client.get("/recommendations/SKU-COFFEE").json()
    assert body["suggestions"], "the suggestion is still offered"

    sku = body["suggestions"][0]["sku"]
    pending = client.post("/purchase-intents",
                          json={"agent_id": AGENT, "sku": sku}
                          ).json()["awaiting_confirmation"]
    verdict = client.post(f"/intents/{pending['request_id']}/confirm", json={}).json()
    assert not verdict["approved"]
    assert verdict["rule"] == "AMOUNT_EXCEEDS_CAP"
