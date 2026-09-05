"""Unit coverage for `Catalog.rename` and `Catalog.remove`.

Added alongside the admin catalog-management routes in `zerotrust/demo.py`
(add/rename/reprice/delete an item) -- everything else `Catalog` does already
had coverage through `test_checkout.py` and the demo route tests; these two
methods did not exist before this feature.
"""

import pytest

from zerotrust.catalog import Catalog, CatalogItem, ItemNotInCatalog


def _catalog() -> Catalog:
    return Catalog([
        CatalogItem("SKU-X", "Thing", 1_000, description="a thing"),
    ])


def test_rename_changes_the_name_and_nothing_else():
    catalog = _catalog()
    catalog.rename("SKU-X", "Renamed Thing")
    item = catalog.get("SKU-X")
    assert item.name == "Renamed Thing"
    assert item.price_paise == 1_000
    assert item.description == "a thing"
    assert item.sku == "SKU-X"


def test_renaming_an_unknown_sku_raises():
    catalog = _catalog()
    with pytest.raises(KeyError):
        catalog.rename("SKU-NOPE", "Whatever")


def test_remove_deletes_the_item():
    catalog = _catalog()
    catalog.remove("SKU-X")
    assert not catalog.has("SKU-X")
    with pytest.raises(ItemNotInCatalog):
        catalog.get("SKU-X")


def test_removing_an_unknown_sku_raises_item_not_in_catalog():
    catalog = _catalog()
    with pytest.raises(ItemNotInCatalog):
        catalog.remove("SKU-NOPE")


def test_remove_does_not_touch_other_items():
    catalog = Catalog([
        CatalogItem("SKU-X", "Thing", 1_000),
        CatalogItem("SKU-Y", "Other Thing", 2_000),
    ])
    catalog.remove("SKU-X")
    assert catalog.has("SKU-Y")
    assert catalog.get("SKU-Y").price_paise == 2_000
