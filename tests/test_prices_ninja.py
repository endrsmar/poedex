"""Tier 1 — reading poe.ninja's two response shapes, and caching them.

Everything here runs against ``tests/fixtures/prices/``, which was trimmed from live
captures on 2026-08-10. The shapes are the point: a parser proved only against
hand-written JSON is a parser proved against my own assumptions.
"""

from __future__ import annotations

import pytest

from modules.prices.backend.api import PricesError
from modules.prices.backend.ninja import (
    CATALOGUE,
    EXCHANGE_PATH,
    ITEM_PATH,
    PREFETCH,
    PriceLine,
    PriceTable,
    TableStore,
    parse_exchange,
    parse_item_overview,
)
from runtime.storage import Storage
from tests.conftest import FakeClock, price_payload

# -- the catalogue --------------------------------------------------------------


def test_every_prefetched_category_is_in_the_catalogue():
    assert set(PREFETCH) <= set(CATALOGUE)


def test_the_categories_spec_5_1_asks_for_are_all_prefetched():
    """SPEC §5.1's list, item by item, so dropping one is a failing test."""
    required = {
        "currency",
        "fragment",
        "scarab",
        "fossil",
        "essence",
        "oil",
        "delirium_orb",
        "incubator",
        "map",
        "card",
        "unique_weapon",
        "unique_armour",
        "unique_accessory",
        "unique_flask",
        "unique_jewel",
    }
    assert required <= set(PREFETCH)


def test_paths_match_the_measured_routes():
    """SPEC §5.1 guessed these from research; these are the measured ones."""
    assert CATALOGUE["currency"].path == EXCHANGE_PATH == (
        "/poe1/api/economy/exchange/current/overview"
    )
    assert CATALOGUE["map"].path == ITEM_PATH == (
        "/poe1/api/economy/stash/current/item/overview"
    )


# -- exchange overviews ----------------------------------------------------------


def test_exchange_lines_join_names_from_the_items_array():
    lines = parse_exchange(price_payload("exchange-currency.json"), "currency")
    by_name = {line.name: line for line in lines}
    assert "Divine Orb" in by_name
    assert by_name["Divine Orb"].chaos > 1
    # The id is the trade id a `~price N divine` note uses. That equality is what
    # makes tier 0 resolvable without a name table.
    assert by_name["Divine Orb"].trade_id == "divine"


def test_exchange_values_are_chaos():
    payload = price_payload("exchange-currency.json")
    assert payload["core"]["primary"] == "chaos"
    lines = parse_exchange(payload, "currency")
    assert all(line.chaos > 0 for line in lines)


def test_an_exchange_overview_in_another_currency_is_refused():
    """Rather than emitting numbers in a unit nothing else in the module uses."""
    payload = dict(price_payload("exchange-currency.json"))
    payload["core"] = {**payload["core"], "primary": "exalted"}
    with pytest.raises(PricesError, match="not chaos"):
        parse_exchange(payload, "currency")


@pytest.mark.parametrize(
    ("fixture", "category", "expect"),
    [
        ("exchange-scarab.json", "scarab", "Divination Scarab of Pilfering"),
        ("exchange-fossil.json", "fossil", "Aberrant Fossil"),
        ("exchange-essence.json", "essence", "Deafening Essence of Greed"),
        ("exchange-oil.json", "oil", "Golden Oil"),
        ("exchange-deliriumorb.json", "delirium_orb", "Fine Delirium Orb"),
        ("exchange-divinationcard.json", "card", "The Apothecary"),
        ("exchange-fragment.json", "fragment", "Sacrifice at Dusk"),
    ],
)
def test_every_exchange_category_parses(fixture, category, expect):
    lines = parse_exchange(price_payload(fixture), category)
    assert expect in {line.name for line in lines}
    assert all(line.category == category for line in lines)


def test_lines_without_a_usable_value_are_dropped():
    payload = {
        "core": {"primary": "chaos"},
        "items": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
        "lines": [
            {"id": "a", "primaryValue": 0},
            {"id": "b", "primaryValue": "nonsense"},
            {"id": "c", "primaryValue": 5},
            "not an object",
        ],
    }
    lines = parse_exchange(payload, "currency")
    # Only `c` survives, and it falls back to its id because no name was supplied.
    assert [(line.name, line.chaos) for line in lines] == [("c", 5.0)]


def test_a_non_object_payload_is_an_error_not_an_empty_table():
    with pytest.raises(PricesError):
        parse_exchange([1, 2, 3], "currency")
    with pytest.raises(PricesError):
        parse_item_overview("nope", "map")


# -- item overviews ---------------------------------------------------------------


def test_item_lines_carry_base_type_and_links():
    lines = parse_item_overview(price_payload("item-uniqueweapon.json"), "unique_weapon")
    pillars = [line for line in lines if line.name == "Pillar of the Caged God"]
    # Two base types times three link counts is exactly the ambiguity `choose_line`
    # exists to resolve.
    assert {(line.base_type, line.links) for line in pillars} >= {
        ("Long Staff", 6),
        ("Iron Staff", 5),
        ("Iron Staff", None),
    }


def test_map_lines_keep_the_tier_that_is_in_their_name():
    lines = parse_item_overview(price_payload("item-map.json"), "map")
    assert "Map (Tier 16)" in {line.name for line in lines}
    # Thirteen map series, one name. The variant is what tells them apart.
    tier16 = [line for line in lines if line.name == "Map (Tier 16)"]
    assert len(tier16) > 5
    assert len({line.variant for line in tier16}) == len(tier16)


def test_an_explicit_map_tier_field_is_folded_into_the_name():
    """`mapTier` is documented but absent from live data. Handle it if it returns."""
    lines = parse_item_overview(
        {"lines": [{"name": "Grotto Map", "mapTier": 9, "chaosValue": 3.0}]}, "map"
    )
    assert lines[0].name == "Grotto Map (Tier 9)"


def test_item_lines_without_a_price_are_dropped():
    lines = parse_item_overview(
        {
            "lines": [
                {"name": "Priced", "chaosValue": 2.0},
                {"name": "Free", "chaosValue": 0},
                {"name": "Broken"},
                {"chaosValue": 5.0},
            ]
        },
        "map",
    )
    assert [line.name for line in lines] == ["Priced"]


def test_corrupted_is_read_as_a_tristate():
    """Absent means "this category does not distinguish", not "not corrupted"."""
    lines = parse_item_overview(
        {
            "lines": [
                {"name": "A", "chaosValue": 1.0, "corrupted": True},
                {"name": "B", "chaosValue": 1.0, "corrupted": False},
                {"name": "C", "chaosValue": 1.0},
            ]
        },
        "map",
    )
    assert [line.corrupted for line in lines] == [True, False, None]


def test_line_detail_describes_which_line_it_is():
    line = PriceLine(
        name="Pillar of the Caged God",
        chaos=50.0,
        category="unique_weapon",
        base_type="Iron Staff",
        links=5,
    )
    assert line.detail() == "Iron Staff, 5L"
    assert PriceLine(name="Golden Oil", chaos=3.0, category="oil").detail() == ""


# -- the table and its store ------------------------------------------------------


def _table(now: float = 1000.0) -> PriceTable:
    return PriceTable(
        category="currency",
        league="Standard",
        lines=parse_exchange(price_payload("exchange-currency.json"), "currency"),
        fetched_at=now,
        etag="W/abc",
    )


def test_a_table_indexes_by_name_and_by_trade_id():
    table = _table()
    assert table.by_name["divine orb"][0].trade_id == "divine"
    assert table.by_trade_id["divine"].name == "Divine Orb"


def test_a_table_round_trips_through_the_store(tmp_path):
    store = TableStore(Storage(tmp_path, "prices"), clock=FakeClock(1000.0))
    table = _table()
    store.save(table)
    loaded = store.load("Standard", "currency")
    assert loaded is not None
    assert loaded.etag == "W/abc"
    assert loaded.fetched_at == table.fetched_at
    assert len(loaded.lines) == len(table.lines)
    # The indexes are rebuilt, not stored.
    assert loaded.by_trade_id["divine"].chaos == table.by_trade_id["divine"].chaos


def test_the_store_keeps_leagues_apart():
    """Standard prices are not challenge-league prices, and confusing the two is the
    kind of error that reads as plausible for a whole league."""
    assert TableStore.filename("Standard", "currency") != TableStore.filename(
        "Allflame", "currency"
    )


def test_a_table_from_another_league_is_not_served(tmp_path):
    store = TableStore(Storage(tmp_path, "prices"))
    store.save(_table())
    assert store.load("Allflame", "currency") is None


def test_a_corrupt_cache_file_is_discarded_not_fatal(tmp_path):
    storage = Storage(tmp_path, "prices")
    store = TableStore(storage)
    storage.write_bytes(TableStore.filename("Standard", "currency"), b"{ not json")
    assert store.load("Standard", "currency") is None


def test_a_cache_file_from_an_older_shape_is_discarded():
    assert PriceTable.from_json({"version": 0, "lines": []}) is None
    assert PriceTable.from_json(None) is None
    assert PriceTable.from_json({"version": 1, "lines": "nope"}) is None


def test_age_never_goes_negative():
    table = _table(now=1000.0)
    assert table.age(900.0) == 0.0
    assert table.age(1300.0) == 300.0
