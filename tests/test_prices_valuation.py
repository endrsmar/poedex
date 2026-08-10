"""Resolution order, matching, deduplication, stack maths.

These run against a :class:`PriceIndex` built directly from the fixtures, with no
module and no transport, because the questions here are arithmetic and lookup rather
than plumbing — ``test_prices_module`` covers the plumbing.
"""

from __future__ import annotations

import json

import pytest

from modules.poeapi.backend.api import NormalizedItem, Source
from modules.poeapi.backend.normalize import normalize_items
from modules.prices.backend.api import PriceSource
from modules.prices.backend.ninja import (
    CATALOGUE,
    PriceTable,
    parse_exchange,
    parse_item_overview,
)
from modules.prices.backend.valuation import (
    PriceIndex,
    candidate_tables,
    choose_line,
    lookup_keys,
    price_key,
)
from tests.conftest import NINJA_TABLES, PRICE_FIXTURES, price_payload

NOW = 1_760_000_000.0

# The chaos values the fixtures actually carry, read once so the expectations below
# are arithmetic on real numbers rather than magic constants.
CURRENCY = {
    line.name: line.chaos
    for line in parse_exchange(price_payload("exchange-currency.json"), "currency")
}
DIVINE = CURRENCY["Divine Orb"]


@pytest.fixture(scope="module")
def index() -> PriceIndex:
    """Every fixture table, loaded, as the module would hold them after a prefetch."""
    tables = {}
    for (kind, type_name), fixture in NINJA_TABLES.items():
        category = next(
            c for c in CATALOGUE.values() if c.kind == kind and c.type == type_name
        )
        parse = parse_exchange if kind == "exchange" else parse_item_overview
        tables[category.key] = PriceTable(
            category=category.key,
            league="Standard",
            lines=parse(price_payload(fixture), category.key),
            fetched_at=NOW,
        )
    return PriceIndex(tables=tables, league="Standard")


@pytest.fixture(scope="module")
def bag() -> dict[str, NormalizedItem]:
    """The fixture bag, normalized, keyed by name for readable assertions."""
    raw = json.loads((PRICE_FIXTURES / "bag.json").read_text("utf-8"))["items"]
    items = normalize_items(raw, source=Source.BAG, split_equipment=True)
    return {item.name: item for item in items if item.location.source is Source.BAG}


@pytest.fixture(scope="module")
def bag_items() -> list[NormalizedItem]:
    raw = json.loads((PRICE_FIXTURES / "bag.json").read_text("utf-8"))["items"]
    items = normalize_items(raw, source=Source.BAG, split_equipment=True)
    return [item for item in items if item.location.source is Source.BAG]


# -- every category resolves ------------------------------------------------------


@pytest.mark.parametrize(
    ("item_name", "table"),
    [
        ("Chaos Orb", "currency"),
        ("Divine Orb", "currency"),
        ("Jeweller's Orb", "currency"),
        ("Sacrifice at Dusk", "fragment"),
        ("Divination Scarab of Pilfering", "scarab"),
        ("Aberrant Fossil", "fossil"),
        ("Deafening Essence of Greed", "essence"),
        ("Golden Oil", "oil"),
        ("Fine Delirium Orb", "delirium_orb"),
        ("Diviner's Incubator", "incubator"),
        ("The Apothecary", "card"),
        ("Grotto Map", "map"),
        ("Tabula Rasa", "unique_armour"),
        ("Pillar of the Caged God", "unique_weapon"),
        ("Headhunter", "unique_accessory"),
    ],
)
def test_every_category_in_spec_5_1_resolves(index, bag, item_name, table):
    """One case per category poe.ninja is asked to cover.

    Scarabs, fossils, essences, oils, delirium orbs and incubators are all
    ``frameType: 5`` and therefore all land in the ``currency`` category on the way
    in — this is the test that they still each find their own table.
    """
    line = index.market_line(bag[item_name])
    assert line is not None, f"{item_name} did not resolve"
    assert line.category == table
    assert line.chaos > 0


def test_a_unique_flask_and_jewel_resolve_too(index):
    """Not in the bag fixture, so exercised directly rather than left uncovered."""
    for name, base, category, table in [
        ("Rumi's Concoction", "Granite Flask", "flask", "unique_flask"),
        ("Watcher's Eye", "Prismatic Jewel", "jewel", "unique_jewel"),
        ("The Tower of Ordeals", "Map (Tier 16)", "map", "unique_map"),
    ]:
        item = _unique(name, base, category)
        line = index.market_line(item)
        assert line is not None and line.category == table, name


# -- map lookup: name, then tier ---------------------------------------------------


def test_a_map_resolves_by_tier_when_its_name_is_not_indexed(index, bag):
    """poe.ninja indexes ordinary maps of a tier as one line, not by area name."""
    grotto = bag["Grotto Map"]
    assert grotto.map_tier == 16
    assert lookup_keys(grotto) == ["Grotto Map (Tier 16)", "Grotto Map", "Map (Tier 16)"]
    line = index.market_line(grotto)
    assert line is not None and line.name == "Map (Tier 16)"


def test_the_map_series_with_the_most_listings_wins(index, bag):
    """Thirteen ``Map (Tier 16)`` lines, one per map series, spanning 1c to 898c.

    Nothing here knows what a map series is. Liquidity is the proxy: the current
    series is the one people are actually trading.
    """
    tier16 = index.table("map").by_name["map (tier 16)"]
    chosen = index.market_line(bag["Grotto Map"])
    assert chosen is not None
    assert chosen.listing_count == max(line.listing_count for line in tier16)
    assert chosen.chaos < max(line.chaos for line in tier16)


def test_a_map_without_a_tier_falls_back_to_its_name(index):
    item = _item(name="Grotto Map", base="Grotto Map", category="map", map_tier=None)
    assert lookup_keys(item) == ["Grotto Map"]
    assert index.market_line(item) is None


# -- unique lookup: links, base type, variant ---------------------------------------


def test_links_and_base_type_pick_the_right_unique_line(index, bag):
    """``Pillar of the Caged God`` is listed six times: two base types times three
    link counts, from 0.96c to 718,160c. Getting this wrong is a 750,000x error."""
    pillar = bag["Pillar of the Caged God"]
    assert pillar.base_type == "Iron Staff"
    assert pillar.sockets.links == 5
    line = index.market_line(pillar)
    assert line is not None
    assert (line.base_type, line.links) == ("Iron Staff", 5)


def test_a_six_link_is_not_priced_as_the_linkless_line(index, bag):
    tabula = bag["Tabula Rasa"]
    line = index.market_line(tabula)
    assert line is not None and line.links == 6


def test_a_linkless_unique_matches_the_line_without_links(index, bag):
    line = index.market_line(bag["Headhunter"])
    assert line is not None and line.links is None and line.base_type == "Leather Belt"


def test_a_corruption_mismatch_is_refused():
    item = _item(name="Thing", base="Base", category="map", corrupted=False)
    lines = [
        _line("Thing", 100.0, corrupted=True, listing_count=9999),
        _line("Thing", 5.0, corrupted=False, listing_count=1),
    ]
    assert choose_line(lines, item).chaos == 5.0
    corrupted = _item(name="Thing", base="Base", category="map", corrupted=True)
    assert choose_line(lines, corrupted).chaos == 100.0


def test_choose_line_on_nothing_is_none():
    assert choose_line([], _item(name="x", base="x", category="map")) is None


def test_routing_prefers_the_unique_table_for_its_category():
    item = _unique("Anything", "Leather Belt", "accessory")
    assert candidate_tables(item) == ("unique_accessory",)
    currency = _item(name="Chaos Orb", base="Chaos Orb", category="currency")
    assert candidate_tables(currency)[0] == "currency"


def test_routing_falls_through_to_other_tables(index):
    """An essence arrives categorised as ``currency`` and must still be found."""
    essence = _item(
        name="Deafening Essence of Greed", base="Deafening Essence of Greed",
        category="currency",
    )
    line = index.market_line(essence)
    assert line is not None and line.category == "essence"


# -- tier 0 before tier 1 ------------------------------------------------------------


def test_a_note_beats_the_market(index, bag):
    """Astramentis is in the index at 30c; the player's note asks half a divine."""
    astramentis = bag["Astramentis"]
    result = index.value(astramentis)
    assert result.source is PriceSource.NOTE
    assert result.price.chaos == pytest.approx(0.5 * DIVINE)
    # And the market price is still there, because Phase 5 wants both.
    assert result.market is not None
    assert result.market.chaos == pytest.approx(30.0)
    assert result.overpriced_by == pytest.approx(0.5 * DIVINE / 30.0)


def test_a_note_prices_an_item_the_market_has_never_heard_of(index, bag):
    ring = bag["Corpse Loop"]
    result = index.value(ring)
    assert result.source is PriceSource.NOTE
    assert result.price.chaos == pytest.approx(25.0)
    assert result.market is None
    assert result.overpriced_by is None


def test_a_divine_note_converts_at_the_table_rate(index, bag):
    result = index.value(bag["Brood Locket"])
    assert result.price.chaos == pytest.approx(3 * DIVINE)


def test_a_malformed_note_falls_through_to_unpriceable(index, bag):
    """``~b/o make me an offer`` must not become zero."""
    result = index.value(bag["Woe Barrage"])
    assert result.unpriceable
    assert result.total_chaos == 0.0
    assert result.note_price is None
    assert "not in the poe.ninja index" in result.reason


def test_a_note_in_an_unknown_currency_says_so(index, bag):
    result = index.value(bag["Dread Nature"])
    assert result.unpriceable
    assert "unobtainium" in result.reason


# -- unpriceable is not zero ----------------------------------------------------------


def test_a_removed_item_is_unpriceable_not_worthless(index, bag):
    """research-notes §7: ~170 Veiled Scarab, absent from the league index."""
    scarab = bag["Veiled Scarab"]
    result = index.value(scarab)
    assert result.unpriceable
    assert result.source is PriceSource.NONE
    assert result.price is None
    assert result.total_chaos == 0.0


def test_the_bag_reports_unpriceable_units_not_just_rows(index, bag_items):
    result = index.value_all(bag_items)
    veiled = next(v for v in result.unpriceable if v.name == "Veiled Scarab")
    assert veiled.stack_size == 170
    assert result.unpriceable_stack >= 170
    assert len(result.unpriceable) < result.unpriceable_stack


# -- stacks and deduplication -----------------------------------------------------------


def test_value_is_per_stack(index, bag):
    """`Jeweller's Orb x2615` and `Divine Orb x5` are one row each and nothing alike."""
    jewellers = index.value(bag["Jeweller's Orb"])
    divine = index.value(bag["Divine Orb"])
    assert jewellers.stack_size == 2615
    assert jewellers.total_chaos == pytest.approx(2615 * CURRENCY["Jeweller's Orb"])
    assert divine.total_chaos == pytest.approx(5 * DIVINE)
    assert divine.total_chaos > jewellers.total_chaos


def test_identical_items_are_looked_up_once(index, bag_items):
    """Three separate Chaos Orb stacks, one price resolution."""
    chaos_rows = [i for i in bag_items if i.name == "Chaos Orb"]
    assert len(chaos_rows) == 3
    result = index.value_all(bag_items)
    assert result.lookups < len(bag_items)
    # 24 rows in, one lookup collapsed away: the three Chaos Orb stacks.
    assert result.lookups == len({price_key(item) for item in bag_items})


def test_deduplication_does_not_merge_the_rows_themselves(index, bag_items):
    """One valuation per item, so the grid can still be drawn slot by slot."""
    result = index.value_all(bag_items)
    assert len(result.items) == len(bag_items)
    assert len({v.uid for v in result.items}) == len(bag_items)
    chaos = [v for v in result.items if v.name == "Chaos Orb"]
    assert [v.stack_size for v in chaos] == [23, 40, 7]
    assert sum(v.total_chaos for v in chaos) == pytest.approx(70 * CURRENCY["Chaos Orb"])


def test_the_price_key_ignores_what_cannot_change_a_price(bag):
    """Two stacks of the same currency share a key; a 5-link and a 6-link do not."""
    tabula = bag["Tabula Rasa"]
    five = tabula.model_copy(update={"sockets": tabula.sockets.model_copy(update={"links": 5})})
    assert price_key(tabula) != price_key(five)
    bigger = tabula.model_copy(update={"stack_size": 9, "uid": "other"})
    assert price_key(tabula) == price_key(bigger)


# -- the bag total ------------------------------------------------------------------------


def test_the_total_is_the_sum_of_the_line_totals(index, bag_items):
    result = index.value_all(bag_items)
    assert result.total_chaos == pytest.approx(sum(v.total_chaos for v in result.items))
    assert result.divine_rate == pytest.approx(DIVINE)
    assert result.total_divine == pytest.approx(result.total_chaos / DIVINE)


def test_a_valuation_pass_issues_no_trade_requests(index, bag_items):
    assert index.value_all(bag_items).trade_requests == 0


def test_the_result_is_json_serializable(index, bag_items):
    """Everything crossing to the frontend is plain JSON (SPEC §4.5)."""
    payload = index.value_all(bag_items).to_json()
    assert json.loads(json.dumps(payload))["priced_count"] > 0


def test_with_no_tables_only_chaos_denominated_notes_survive(bag_items):
    """A table-less start is degraded, not broken — and honestly so.

    Chaos is the unit rather than a table entry, so ``~b/o 25 chaos`` still resolves
    while ``~price 3 divine`` cannot: the divine rate lives in the currency table.
    Everything else is unpriceable, and the divine total is ``None`` rather than a
    number computed from a rate nobody has.
    """
    empty = PriceIndex(tables={}, league="Standard")
    result = empty.value_all(bag_items)
    priced = {v.name for v in result.priced}
    assert priced == {"Corpse Loop"}
    assert result.total_chaos == pytest.approx(25.0)
    assert len(result.unpriceable) == len(bag_items) - 1
    assert result.divine_rate is None
    assert result.total_divine is None


def test_chaos_is_its_own_unit():
    empty = PriceIndex(tables={}, league="Standard")
    assert empty.chaos_for_trade_id("chaos") == 1.0
    assert empty.chaos_for_trade_id("divine") is None


# -- helpers --------------------------------------------------------------------------------


def _item(**overrides) -> NormalizedItem:
    from modules.poeapi.backend.api import Location, Rarity

    fields = {
        "uid": "test",
        "name": "Thing",
        "base_type": "Thing",
        "category": "currency",
        "rarity": Rarity.NORMAL,
        "location": Location(source=Source.BAG),
    }
    fields.update({k: v for k, v in overrides.items() if k not in {"base", "name"}})
    if "name" in overrides:
        fields["name"] = overrides["name"]
    if "base" in overrides:
        fields["base_type"] = overrides["base"]
    return NormalizedItem(**fields)


def _unique(name: str, base: str, category: str) -> NormalizedItem:
    from modules.poeapi.backend.api import Rarity

    return _item(name=name, base=base, category=category, rarity=Rarity.UNIQUE)


def _line(name: str, chaos: float, **kwargs):
    from modules.prices.backend.ninja import PriceLine

    return PriceLine(name=name, chaos=chaos, category="test", **kwargs)
