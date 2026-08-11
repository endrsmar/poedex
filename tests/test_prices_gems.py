"""Skill gems: matched on the exact variant, fetched only when one is present.

Gems were excluded from pricing since Phase 3, and the reason was right. poe.ninja
prices them per ``level/quality/corrupted`` variant, nothing here scored variants, and
a gem priced as the wrong variant is worse than an unpriced gem — a level 21 / 20%
Cyclone is orders of magnitude above the level 1 that shares its name and its base
type. The measured cost of the exclusion was a live stash tab where **7 of 19 rows**
were gems, all reported `UNPRICEABLE`.

What changes is the matching, not the caution. :func:`gem_line` matches all three
axes exactly or returns nothing; there is no nearest row, no tie-break on liquidity,
and no fall-through to another table. And the 4.0 MB table is fetched **lazily** — the
first time a bag or tab actually holds a gem — so a league that never shows one never
pays for it.

**The seven live gems still do not price, and that is the correct answer.** Read from
the account on 2026-08-11 they are all level 1 at 5%, 6%, 8%, 13%, 14%, 14% and 17%
quality, and poe.ninja publishes exactly three quality values across the whole table:
0, 20 and 23. There is no row for any of them. What changed is the sentence: they were
"not in the poe.ninja index for this league", and are now "poe.ninja lists no level 1,
14% quality variant of this gem; refusing to price it as a different one" — the
difference between a tool that has not looked and a tool that looked and refused.
"""

from __future__ import annotations

import pytest

from modules.poeapi.backend.api import Source
from modules.poeapi.backend.models import Gem
from modules.poeapi.backend.normalize import normalize_items
from modules.prices.backend.api import PriceSource
from modules.prices.backend.ninja import CATALOGUE, NEVER_PREFETCH, ON_DEMAND, PriceLine
from modules.prices.backend.valuation import (
    GEM_TABLES,
    describe_variant,
    gem_line,
    gem_variant,
    price_key,
    wanted_variant,
)
from tests.conftest import price_payload

NINJA = "poe.ninja"

# The seven gems the live tab held, with the level and quality the account actually
# reported. Not invented: read from `Sext` in Allflame on 2026-08-11.
LIVE_TAB_GEMS = (
    ("Static Strike", 1, 14),
    ("Spirit Offering", 1, 14),
    ("Cyclone", 1, 13),
    ("Blade Flurry", 1, 6),
    ("Penance Brand", 1, 8),
    ("Caustic Arrow", 1, 17),
    ("Contagion", 1, 5),
)


def gem_item(name: str, level: int | None, quality: int, *, corrupted: bool = False):
    """One gem, through the real normalizer, in the shape GGG sends.

    Built from raw wire JSON rather than by constructing a ``NormalizedItem``, because
    half of what is under test is whether ``Level: ["20 (Max)"]`` and
    ``Quality: ["+20%"]`` survive the trip at all.
    """
    properties: list[dict] = [{"name": "Attack, AoE, Melee", "values": [], "displayMode": 0}]
    if level is not None:
        properties.append({"name": "Level", "values": [[str(level), 0]], "type": 5})
    if quality:
        properties.append({"name": "Quality", "values": [[f"+{quality}%", 0]], "type": 6})
    raw = {
        "id": f"gem-{name}-{level}-{quality}-{corrupted}",
        "verified": True,
        "w": 1,
        "h": 1,
        "icon": "https://web.poecdn.com/image/Art/2DItems/Gems/Cyclone.png",
        "name": "",
        "typeLine": name,
        "baseType": name,
        "frameType": 4,
        "identified": True,
        "ilvl": 0,
        "properties": properties,
        "x": 0,
        "y": 0,
        "inventoryId": "MainInventory",
        **({"corrupted": True} if corrupted else {}),
    }
    return normalize_items([raw], source=Source.BAG)[0]


# -- reading the item ------------------------------------------------------------


def test_the_normalizer_reads_level_and_quality_off_the_wire():
    gem = gem_item("Cyclone", 20, 20)
    assert gem.category == "gem"
    assert gem.gem == Gem(level=20, quality=20)


def test_quality_absent_is_zero_and_level_absent_is_unknown():
    """Two absences that must not be read the same way.

    GGG omits ``Quality`` on a 0% gem rather than sending ``+0%``, so absent quality
    is a fact. Absent level is not: it is the axis worth three orders of magnitude,
    and defaulting it to 1 is how a level 21 gem gets priced as a level 1.
    """
    assert gem_item("Cyclone", 20, 0).gem == Gem(level=20, quality=0)
    unreadable = gem_item("Cyclone", None, 20)
    assert unreadable.gem == Gem(level=None, quality=20)
    assert wanted_variant(unreadable) is None


def test_the_max_suffix_on_a_capped_gem_is_display_text():
    """``"20 (Max)"`` is what GGG sends for a gem at its level cap."""
    raw = {
        "id": "capped",
        "typeLine": "Cyclone",
        "baseType": "Cyclone",
        "frameType": 4,
        "icon": "https://web.poecdn.com/image/Art/2DItems/Gems/Cyclone.png",
        "properties": [
            {"name": "Level", "values": [["20 (Max)", 0]], "type": 5},
            {"name": "Quality", "values": [["+20%", 0]], "type": 6},
        ],
    }
    assert normalize_items([raw], source=Source.BAG)[0].gem == Gem(level=20, quality=20)


def test_a_non_gem_has_no_gem_block_at_all():
    """``None`` says "not a gem", which is a different claim from "a gem with no
    level" — and `prices` branches on the difference."""
    raw = {
        "id": "ring",
        "typeLine": "Sapphire Ring",
        "baseType": "Sapphire Ring",
        "frameType": 2,
        "icon": "https://web.poecdn.com/image/Art/2DItems/Rings/Sapphire.png",
    }
    item = normalize_items([raw], source=Source.BAG)[0]
    assert item.category != "gem"
    assert item.gem is None


# -- reading the table -----------------------------------------------------------


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("1", (1, 0, False)),
        ("20", (20, 0, False)),
        ("20/20", (20, 20, False)),
        ("21c", (21, 0, True)),
        ("21/23c", (21, 23, True)),
        ("1/20", (1, 20, False)),
    ],
)
def test_the_whole_variant_grammar(variant: str, expected: tuple[int, int, bool]):
    """Every shape poe.ninja's SkillGem table uses, and it uses no others.

    Measured against Allflame on 2026-08-11: 7 519 rows, 27 distinct variants, all
    four of these shapes and nothing else.
    """
    line = PriceLine(
        name="Cyclone",
        chaos=1.0,
        category="skill_gem",
        variant=variant,
        corrupted=expected[2] or None,
        gem_level=expected[0],
    )
    assert gem_variant(line) == expected


@pytest.mark.parametrize("variant", ["", "20/", "/20", "20x", "20/20/20", "c", "alt", "6L"])
def test_a_variant_that_is_not_the_grammar_is_not_guessed_at(variant: str):
    line = PriceLine(name="Cyclone", chaos=1.0, category="skill_gem", variant=variant)
    assert gem_variant(line) is None


def test_a_row_that_contradicts_itself_is_dropped_rather_than_resolved():
    """``variant`` and ``gemLevel`` disagreeing is not a thing to pick a winner in.

    Both encode the level and across 7 519 real rows they never disagreed. If one
    ever does, the row does not describe a single gem, and preferring either field
    would be inventing which gem it is.
    """
    contradiction = PriceLine(
        name="Cyclone", chaos=1.0, category="skill_gem", variant="21/20c", gem_level=20
    )
    assert gem_variant(contradiction) is None

    corruption = PriceLine(
        name="Cyclone", chaos=1.0, category="skill_gem", variant="21/20c", corrupted=False
    )
    assert gem_variant(corruption) is None


def test_the_fixture_is_the_real_table_and_carries_the_real_grammar():
    from modules.prices.backend.ninja import parse_item_overview

    lines = parse_item_overview(price_payload("item-skillgem.json"), "skill_gem")
    assert len(lines) > 50
    assert all(gem_variant(line) is not None for line in lines)
    # The grid is sparse, and that is the fact the whole design rests on.
    qualities = {gem_variant(line)[1] for line in lines}
    assert qualities == {0, 20, 23}, qualities


# -- matching --------------------------------------------------------------------


@pytest.fixture(scope="module")
def gem_lines():
    from modules.prices.backend.ninja import parse_item_overview

    return parse_item_overview(price_payload("item-skillgem.json"), "skill_gem")


def _named(lines, name: str):
    return [line for line in lines if line.name == name]


def test_each_axis_selects_a_different_row(gem_lines):
    """Level, quality and corruption each move the answer on their own."""
    rows = _named(gem_lines, "Cyclone")
    prices = {}
    for level, quality, corrupted in ((1, 0, False), (20, 20, False), (21, 20, True)):
        matched = gem_line(rows, gem_item("Cyclone", level, quality, corrupted=corrupted))
        assert matched is not None, (level, quality, corrupted)
        prices[(level, quality, corrupted)] = matched.chaos
    assert len(set(prices.values())) == 3, prices
    # The direction is not asserted as a constant — it is read off the table — but a
    # level 21 corrupted gem being cheaper than a level 1 would mean the match is
    # crossed over, which is the failure worth catching.
    assert prices[(21, 20, True)] > prices[(1, 0, False)]


def test_corruption_alone_changes_which_row_is_returned(gem_lines):
    rows = _named(gem_lines, "Vaal Cyclone")
    plain = gem_line(rows, gem_item("Vaal Cyclone", 20, 20))
    corrupted = gem_line(rows, gem_item("Vaal Cyclone", 20, 20, corrupted=True))
    # The fixture carries `20/20c` and no `20/20`. So the uncorrupted gem has no row
    # at all, and must not borrow the corrupted one.
    assert plain is None
    assert corrupted is not None and corrupted.variant == "20/20c"


def test_a_variant_the_table_lacks_is_unpriceable_not_a_near_miss(gem_lines):
    """The whole point. Level 19 / 12% Cyclone is not in the table; the nearest row
    by any metric is 20/20, and it is worth many times more."""
    rows = _named(gem_lines, "Cyclone")
    assert {gem_variant(line) for line in rows}.isdisjoint({(19, 12, False)})
    assert gem_line(rows, gem_item("Cyclone", 19, 12)) is None


def test_a_gem_with_no_readable_level_is_unpriceable(gem_lines):
    assert gem_line(_named(gem_lines, "Cyclone"), gem_item("Cyclone", None, 20)) is None


def test_two_rows_claiming_one_variant_is_a_refusal_rather_than_a_choice():
    """poe.ninja has never produced one. If it did, picking would be a guess."""
    twin = [
        PriceLine(name="Cyclone", chaos=1.0, category="skill_gem", variant="20/20", gem_level=20),
        PriceLine(name="Cyclone", chaos=99.0, category="skill_gem", variant="20/20", gem_level=20),
    ]
    assert gem_line(twin, gem_item("Cyclone", 20, 20)) is None


def test_a_transfigured_gem_is_its_own_name_not_a_variant(gem_lines):
    """Alternate-quality gems are gone; transfigured gems replaced them and they are
    separate rows under separate names. So they need no fourth axis — they match by
    name like anything else, and must not collapse into the base gem."""
    base = gem_line(_named(gem_lines, "Blade Flurry"), gem_item("Blade Flurry", 20, 20))
    transfigured = gem_line(
        _named(gem_lines, "Blade Flurry of Incision"),
        gem_item("Blade Flurry of Incision", 20, 20),
    )
    assert base is not None and transfigured is not None
    assert base.chaos != transfigured.chaos


def test_two_gems_of_one_name_and_two_levels_do_not_share_a_lookup():
    """Deduplication is by ``price_key``, and a gem's level belongs in it.

    Without this a tab holding a level 1 and a level 21 Cyclone would value the
    second as the first — the same wrong answer arriving through the cache instead of
    through the match.
    """
    assert price_key(gem_item("Cyclone", 1, 0)) != price_key(gem_item("Cyclone", 21, 20))
    assert price_key(gem_item("Cyclone", 20, 0)) != price_key(gem_item("Cyclone", 20, 20))
    assert price_key(gem_item("Cyclone", 20, 20)) == price_key(gem_item("Cyclone", 20, 20))


# -- the live tab ----------------------------------------------------------------


@pytest.mark.parametrize("name,level,quality", LIVE_TAB_GEMS)
def test_the_live_tabs_gems_are_refused_with_a_reason_that_names_the_variant(
    gem_lines, name, level, quality
):
    """Seven real gems, and the honest outcome is still `unpriceable`.

    Every one is level 1 at a quality poe.ninja does not publish a row for — it
    publishes 0, 20 and 23, and these are 5, 6, 8, 13, 14, 14 and 17. Pricing them
    off the ``1`` or ``1/20`` row is precisely the substitution the exclusion existed
    to prevent, so the fix is the *sentence*, not a number.
    """
    from modules.prices.backend.ninja import PriceTable
    from modules.prices.backend.valuation import PriceIndex

    item = gem_item(name, level, quality)
    table = PriceTable(
        category="skill_gem", league="Allflame", lines=gem_lines, fetched_at=1_760_000_000.0
    )
    index = PriceIndex(tables={"skill_gem": table}, league="Allflame")
    valuation = index.value(item)

    assert valuation.unpriceable
    assert valuation.price is None
    assert f"level {level}" in valuation.reason
    assert f"{quality}% quality" in valuation.reason
    assert "refusing to price it as a different one" in valuation.reason


def test_a_listed_variant_of_the_same_seven_gems_does_price(gem_lines):
    """The mechanism works; it is the tab's gems that the table has no row for.

    The same seven names at a variant poe.ninja *does* publish all resolve, which is
    what separates "the matching is broken" from "these particular gems are not
    listed".
    """
    from modules.prices.backend.ninja import PriceTable
    from modules.prices.backend.valuation import PriceIndex

    table = PriceTable(
        category="skill_gem", league="Allflame", lines=gem_lines, fetched_at=1_760_000_000.0
    )
    index = PriceIndex(tables={"skill_gem": table}, league="Allflame")
    for name, _level, _quality in LIVE_TAB_GEMS:
        valuation = index.value(gem_item(name, 20, 20))
        assert not valuation.unpriceable, (name, valuation.reason)
        assert valuation.price is not None and valuation.price.source is PriceSource.BULK
        assert valuation.price.chaos > 0


def test_the_three_silences_are_told_apart(gem_lines):
    """"Unpriceable" was one word for three different situations."""
    from modules.prices.backend.ninja import PriceTable
    from modules.prices.backend.valuation import PriceIndex

    empty = PriceIndex(tables={}, league="Allflame")
    assert "has not been loaded" in empty.value(gem_item("Cyclone", 20, 20)).reason

    table = PriceTable(
        category="skill_gem", league="Allflame", lines=gem_lines, fetched_at=1_760_000_000.0
    )
    loaded = PriceIndex(tables={"skill_gem": table}, league="Allflame")
    assert "no gem level on this item" in loaded.value(gem_item("Cyclone", None, 20)).reason
    assert "lists no level 19" in loaded.value(gem_item("Cyclone", 19, 12)).reason


def test_describe_variant_reads_like_the_item():
    assert describe_variant(gem_item("Cyclone", 21, 20, corrupted=True)) == (
        "level 21, 20% quality, corrupted"
    )
    assert describe_variant(gem_item("Cyclone", 1, 0)) == "level 1, 0% quality"
    assert describe_variant(gem_item("Cyclone", None, 0)) == "no readable gem level"


# -- a gem never borrows another table's answer ----------------------------------


def test_a_gem_is_never_priced_out_of_another_table(gem_lines):
    """Every other category falls through to "try every loaded table", which is safe
    when names are unique across them. For a gem it is not: a name-only hit somewhere
    else is exactly the confident wrong answer."""
    from modules.prices.backend.ninja import PriceTable
    from modules.prices.backend.valuation import PriceIndex

    impostor = PriceTable(
        category="unique_jewel",
        league="Allflame",
        lines=[PriceLine(name="Cyclone", chaos=5000.0, category="unique_jewel")],
        fetched_at=1_760_000_000.0,
    )
    index = PriceIndex(tables={"unique_jewel": impostor}, league="Allflame")
    assert index.value(gem_item("Cyclone", 20, 20)).unpriceable


def test_gem_tables_is_exactly_what_the_catalogue_calls_it():
    assert all(key in CATALOGUE for key in GEM_TABLES)
    assert set(GEM_TABLES) <= set(ON_DEMAND)
    assert set(ON_DEMAND) <= set(NEVER_PREFETCH)
    assert all(reason.strip() for reason in ON_DEMAND.values())


# -- lazy, which is what keeps 4 MB off every startup ----------------------------


def _gem_requests(server) -> list:
    return [r for r in server.to_host(NINJA) if "SkillGem" in str(r.url)]


async def test_a_prefetch_does_not_fetch_the_gem_table(priced_stack, server, prices_module):
    """The whole reason it can be enabled at all.

    4.0 MB and 7 519 rows, on every league, on a 30-minute cycle, to answer a
    question most bags never ask. Discovery does not probe it and the refresh does
    not want it, so a cold start costs exactly what it did before.
    """
    assert _gem_requests(server) == []
    assert prices_module.index().table("skill_gem") is None
    status = prices_module.status()
    assert status.healthy and status.loaded == status.requested


async def test_a_bag_with_no_gem_in_it_still_does_not(priced_stack, server, priced):
    from modules.prices.backend.api import LeagueSource

    before = len(_gem_requests(server))
    ring = normalize_items(
        [
            {
                "id": "ring-1",
                "typeLine": "Sapphire Ring",
                "baseType": "Sapphire Ring",
                "frameType": 2,
                "icon": "https://web.poecdn.com/image/Art/2DItems/Rings/Sapphire.png",
            }
        ],
        source=Source.BAG,
    )
    result = await priced.value_all(ring, league="Standard")
    assert result.league_source in set(LeagueSource)
    assert len(_gem_requests(server)) == before


async def test_one_gem_in_the_bag_fetches_the_table_once(priced_stack, server, priced):
    """Lazy means *on the first gem*, and then not again."""
    before = len(_gem_requests(server))
    bag = [gem_item("Cyclone", 20, 20), gem_item("Blade Flurry", 21, 20, corrupted=True)]

    first = await priced.value_all(bag, league="Standard")
    assert len(_gem_requests(server)) == before + 1
    assert all(not item.unpriceable for item in first.items), [i.reason for i in first.items]

    second = await priced.value_all(bag, league="Standard")
    assert len(_gem_requests(server)) == before + 1, "the second pass refetched it"
    assert [i.total_chaos for i in second.items] == [i.total_chaos for i in first.items]


async def test_the_gem_table_joins_the_refresh_cycle_only_after_it_is_loaded(
    priced_stack, server, priced, prices_module
):
    """Before: not wanted, so not refreshed. After: wanted, so kept fresh.

    Leaving it out of the cycle forever would be the other mistake — 4 MB of gem
    prices going stale on disk while every other table is current. Rejoining costs a
    conditional GET, which is a 304.
    """
    assert not any(c.key == "skill_gem" for c in prices_module._wanted())
    await priced.value_all([gem_item("Cyclone", 20, 20)], league="Standard")
    assert any(c.key == "skill_gem" for c in prices_module._wanted())
    status = prices_module.status()
    assert status.loaded == status.requested


async def test_a_gem_the_table_cannot_answer_still_costs_only_the_one_fetch(
    priced_stack, server, priced
):
    """The seven live gems, end to end through the module.

    Unpriceable is the outcome, and it must not become a retry loop: the table is
    fetched once, and every one of them is refused by variant against it.
    """
    before = len(_gem_requests(server))
    bag = [gem_item(name, level, quality) for name, level, quality in LIVE_TAB_GEMS]
    result = await priced.value_all(bag, league="Standard")
    assert len(_gem_requests(server)) == before + 1
    assert len(result.unpriceable) == len(LIVE_TAB_GEMS)
    assert all("refusing to price it as a different one" in i.reason for i in result.unpriceable)
