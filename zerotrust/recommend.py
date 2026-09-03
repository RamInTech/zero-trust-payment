"""Complementary-item suggestions — the revenue side of the same boundary.

A SUGGESTION IS A PROPOSAL, EXACTLY LIKE A PARSED INTENT. This module is the
second place in the system where something other than the customer proposes
spending money, and it is deliberately given no more authority than the first.
A `Suggestion` carries a SKU and a reason. It has no field for a price, no
field for an approval, and no way to mark itself pre-approved -- the same
structural argument that keeps `ParsedIntent` harmless, applied to the upsell
path so it cannot become a back door around the mandate.

WHY SUGGESTIONS ARE NOT FILTERED BY THE MANDATE HERE. It would be easy to hide
suggestions the policy engine would refuse, and it would make the demo tidier.
It is the wrong design: the mandate would then be enforced in two places, and
the recommender's copy would be the one nobody tests. The recommender proposes
against the catalog; the policy engine decides. A suggestion that exceeds the
cap gets offered, confirmed by a human if they want it, and then refused --
which is the system working, not a bug in the recommender.

WHY THIS IS NOT AN LLM. Pairing coffee with biscuits is a lookup, not a
language problem. The intent parser earns a model because turning "something
cold to drink" into a SKU genuinely requires one; picking a complement does
not, and reaching for a model here would be the kind of unnecessary AI this
project otherwise avoids on purpose. The `Recommender` protocol leaves the seam
open if that judgement ever changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from zerotrust.catalog import Catalog


@dataclass(frozen=True)
class Suggestion:
    """A proposed add-on. Not an authorisation, and not a price."""

    sku: str
    reason: str
    #: Which purchase prompted it, so the audit trail can connect the two.
    prompted_by_sku: str = ""


@runtime_checkable
class Recommender(Protocol):
    def suggest(self, sku: str, limit: int = 1) -> list[Suggestion]: ...


#: What pairs with what, and the merchant-written reason shown to the customer.
#: Deliberately data, not logic: a merchant editing this list cannot change what
#: an agent is allowed to spend, only what it may bring up.
PAIRINGS: dict[str, list[tuple[str, str]]] = {
    "SKU-COFFEE": [("SKU-BISCUITS", "goes well with filter coffee"),
                   ("SKU-MUG", "something to drink it from")],
    "SKU-TEA": [("SKU-BISCUITS", "the usual companion to chai")],
    "SKU-BEANS": [("SKU-MUG", "for the first cup"),
                  ("SKU-COFFEE", "a ready-ground pack as backup")],
    "SKU-CAKE": [("SKU-COFFEE", "cake is better with coffee")],
    "SKU-MUG": [("SKU-COFFEE", "something to put in it")],
    "SKU-PHONE": [("SKU-CHARGER", "a faster charger than the one in the box"),
                  ("SKU-CABLE", "a spare cable")],
    "SKU-EARPHONES": [("SKU-POWERBANK", "keeps them charged on the move")],
    "SKU-CHARGER": [("SKU-CABLE", "a spare cable to go with it")],
    "SKU-POWERBANK": [("SKU-CABLE", "a spare cable to go with it")],
    "SKU-RICE": [("SKU-OIL", "for the same shelf")],
    "SKU-OIL": [("SKU-RICE", "for the same shelf")],
    "SKU-COLA": [("SKU-CHIPS", "obvious, but people do want it")],
    "SKU-CHIPS": [("SKU-COLA", "obvious, but people do want it")],
    "SKU-NOTEBOOK": [("SKU-PEN", "nothing to write with otherwise")],
    "SKU-PEN": [("SKU-NOTEBOOK", "nothing to write on otherwise")],
}


class StaticRecommender:
    """Deterministic complements, validated against the live catalog.

    The catalog check is not a formality: it is the same boundary the intent
    parsers use. A pairing naming an item that has been removed or is out of
    stock is dropped here rather than becoming a suggestion for something
    nobody can buy.
    """

    name = "static"

    def __init__(self, catalog: Catalog, pairings: Optional[dict] = None) -> None:
        self.catalog = catalog
        self.pairings = PAIRINGS if pairings is None else pairings

    def suggest(self, sku: str, limit: int = 1) -> list[Suggestion]:
        out: list[Suggestion] = []
        for candidate, reason in self.pairings.get(sku, []):
            if len(out) >= limit:
                break
            if candidate == sku:
                # Suggesting the thing just bought is noise, and at worst
                # nudges a duplicate purchase the idempotency layer would
                # then have to sort out.
                continue
            if not self.catalog.has(candidate):
                continue
            if not self.catalog.get(candidate).available:
                continue
            out.append(Suggestion(sku=candidate, reason=reason,
                                  prompted_by_sku=sku))
        return out
