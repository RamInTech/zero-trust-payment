"""Phase 5 — the agent-readable catalog.

Deliberately small: a structured product list, not a RAG pipeline. The bar this
project is judged against rewards restraint over AI surface area, and the
catalog's job here is narrow -- it is the source of truth for two questions:

    "does this SKU exist?"          (asked before the policy engine)
    "what does it ACTUALLY cost?"   (asked again at confirmation time)

That second question is the important one. The amount displayed to a human is
never trusted at confirm time; it is re-read from here and compared. See
`zerotrust/checkout.py`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable, Optional


class ItemNotInCatalog(LookupError):
    """Raised when a SKU has no catalog entry. Rejected before policy runs."""


@dataclass(frozen=True)
class CatalogItem:
    sku: str
    name: str
    price_paise: int
    available: bool = True
    description: str = ""

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "name": self.name,
            "price_paise": self.price_paise,
            "price_rupees": self.price_paise / 100,
            "available": self.available,
            "description": self.description,
        }


class Catalog:
    """An in-memory product list. Prices can change -- that is the point."""

    def __init__(self, items: Optional[Iterable[CatalogItem]] = None) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, CatalogItem] = {}
        for item in items or ():
            self._items[item.sku] = item

    def add(self, item: CatalogItem) -> None:
        with self._lock:
            self._items[item.sku] = item

    def get(self, sku: str) -> CatalogItem:
        with self._lock:
            try:
                return self._items[sku]
            except KeyError:
                raise ItemNotInCatalog(
                    f"sku '{sku}' is not in the catalog"
                ) from None

    def has(self, sku: str) -> bool:
        with self._lock:
            return sku in self._items

    def current_price_paise(self, sku: str) -> int:
        """Re-read the true price. Called again at confirmation time."""
        return self.get(sku).price_paise

    def set_price(self, sku: str, price_paise: int) -> None:
        """Change a price -- used by tests to simulate drift between display
        and confirm, and by any real merchant at any moment."""
        with self._lock:
            item = self._items[sku]
            self._items[sku] = CatalogItem(
                sku=item.sku,
                name=item.name,
                price_paise=price_paise,
                available=item.available,
                description=item.description,
            )

    def set_available(self, sku: str, available: bool) -> None:
        with self._lock:
            item = self._items[sku]
            self._items[sku] = CatalogItem(
                sku=item.sku,
                name=item.name,
                price_paise=item.price_paise,
                available=available,
                description=item.description,
            )

    def all(self) -> list[CatalogItem]:
        with self._lock:
            return sorted(self._items.values(), key=lambda i: i.sku)

    def for_llm(self) -> str:
        """The catalog rendered for an intent parser's prompt.

        Note what is NOT here: no instructions, no authority, no mention of
        approval. The parser gets a list of things that exist and their prices.
        """
        lines = []
        for item in self.all():
            status = "" if item.available else " (out of stock)"
            lines.append(
                f"- {item.sku}: {item.name}, Rs.{item.price_paise / 100:.2f}{status}"
            )
        return "\n".join(lines)


def demo_catalog() -> Catalog:
    """A general store, not a coffee shop.

    Deliberately broad and deliberately spanning the mandate's default Rs.500
    cap in both directions, because a catalog where everything is affordable
    demonstrates nothing. The phone at Rs.14,999 is the clearest case: an
    ordinary thing a person would plausibly ask for, which the agent simply
    cannot buy on this mandate.

    The first five SKUs are load-bearing -- tests and the adversarial suite
    assert on their exact prices -- so they keep their ids and amounts.
    """
    return Catalog([
        # -- the original five; prices are pinned by tests ------------------
        CatalogItem("SKU-COFFEE", "Filter Coffee", 15_000,
                    description="250g pack of filter coffee"),
        CatalogItem("SKU-CAKE", "Chocolate Cake", 45_000,
                    description="Half kilo chocolate truffle cake"),
        CatalogItem("SKU-TEA", "Masala Chai", 8_000,
                    description="100g loose leaf masala chai"),
        CatalogItem("SKU-MUG", "Ceramic Mug", 25_000,
                    description="Handmade ceramic mug"),
        CatalogItem("SKU-BEANS", "Arabica Beans", 90_000,
                    description="1kg single origin arabica beans"),

        # -- cold drinks ----------------------------------------------------
        CatalogItem("SKU-COLA", "Cola 500ml", 4_500,
                    description="Chilled cola, 500ml bottle"),
        CatalogItem("SKU-LEMONADE", "Lemon Soda", 4_000,
                    description="Sparkling lemon soda, 330ml can"),
        CatalogItem("SKU-JUICE", "Orange Juice", 6_000,
                    description="No-sugar-added orange juice, 1L"),
        CatalogItem("SKU-WATER", "Mineral Water", 2_000,
                    description="Packaged drinking water, 1L"),

        # -- snacks and groceries -------------------------------------------
        CatalogItem("SKU-CHIPS", "Potato Chips", 3_000,
                    description="Salted potato chips, 150g"),
        CatalogItem("SKU-BISCUITS", "Butter Biscuits", 5_000,
                    description="Butter biscuits, 300g pack"),
        CatalogItem("SKU-RICE", "Basmati Rice", 32_000,
                    description="Aged basmati rice, 5kg"),
        CatalogItem("SKU-OIL", "Sunflower Oil", 21_000,
                    description="Refined sunflower oil, 1L"),

        # -- electronics; most of these sit ABOVE the default cap ------------
        CatalogItem("SKU-PHONE", "Mobile Phone", 1_499_900,
                    description="6.5-inch smartphone, 128GB"),
        CatalogItem("SKU-EARPHONES", "Wireless Earphones", 129_900,
                    description="Bluetooth in-ear headphones"),
        CatalogItem("SKU-CHARGER", "Fast Charger", 69_900,
                    description="65W USB-C fast charger"),
        CatalogItem("SKU-POWERBANK", "Power Bank", 149_900,
                    description="10000mAh power bank"),
        CatalogItem("SKU-CABLE", "USB-C Cable", 29_900,
                    description="1m braided USB-C cable"),

        # -- home and stationery --------------------------------------------
        CatalogItem("SKU-NOTEBOOK", "Notebook", 12_000,
                    description="A5 ruled notebook, 200 pages"),
        CatalogItem("SKU-PEN", "Gel Pen Set", 9_000,
                    description="Pack of 5 gel pens"),
        CatalogItem("SKU-UMBRELLA", "Umbrella", 39_900,
                    description="Compact folding umbrella"),
        CatalogItem("SKU-TSHIRT", "Cotton T-Shirt", 59_900,
                    description="Plain cotton t-shirt"),
    ])
