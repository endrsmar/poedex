"""Tier 3 — the trade client (SPEC §5.3).

The most important test in this file is the one that does nothing:
``test_a_valuation_pass_issues_zero_trade_requests`` over in ``test_prices_module``.
What is here checks that when it *is* called deliberately, it asks the right
questions and reads the answers the way the spec says to.
"""

from __future__ import annotations

import pytest

from modules.poeapi.backend.api import Location, NormalizedItem, Rarity, Source
from modules.prices.backend.api import ModFocus, TradeUnavailable
from modules.prices.backend.trade import (
    MAX_FETCH_IDS,
    MAX_STAT_FILTERS,
    StatIndex,
    TradeClient,
    build_plan,
    build_query,
    median_of_cheapest,
    normalize_stat_text,
    widened,
)
from runtime.storage import Storage
from tests.conftest import FakeClock, price_payload

GGG = "www.pathofexile.com"


# -- stat ids -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        ("+58 to maximum Life", "+# to maximum Life"),
        ("Adds 12 to 30 Physical Damage", "Adds # to # Physical Damage"),
        ("18% increased Movement Speed", "#% increased Movement Speed"),
        ("1.5% of Damage Leeched", "#% of Damage Leeched"),
        ("Cannot be Frozen", "Cannot be Frozen"),
    ],
)
def test_numbers_collapse_to_hashes(text, expect):
    assert normalize_stat_text(text) == expect


def test_the_index_maps_text_to_an_opaque_id():
    index = StatIndex.from_payload(price_payload("trade-stats.json"), 0.0)
    stat_id = index.stat_id("+58 to maximum Life")
    assert stat_id is not None
    # Opaque, and never the readable text — that is the whole point of the document.
    assert stat_id.startswith(("explicit.", "pseudo.", "implicit."))
    assert "maximum Life" not in stat_id


def test_an_unknown_mod_has_no_id():
    index = StatIndex.from_payload(price_payload("trade-stats.json"), 0.0)
    assert index.stat_id("Grants Level 20 Nonexistent Skill") is None


def test_the_first_group_wins_so_pseudo_does_not_shadow_explicit():
    entry = {"text": "+# Life"}
    payload = {
        "result": [
            {"id": "pseudo", "label": "P", "entries": [{**entry, "id": "pseudo.x"}]},
            {"id": "explicit", "label": "E", "entries": [{**entry, "id": "explicit.x"}]},
        ]
    }
    assert StatIndex.from_payload(payload, 0.0).stat_id("+# Life") == "pseudo.x"


def test_an_empty_stats_document_is_an_error():
    with pytest.raises(TradeUnavailable):
        StatIndex.from_payload({"result": []}, 0.0)
    with pytest.raises(TradeUnavailable):
        StatIndex.from_payload({}, 0.0)


def test_the_index_round_trips():
    original = StatIndex.from_payload(price_payload("trade-stats.json"), 123.0)
    restored = StatIndex.from_json(original.to_json())
    assert restored is not None
    assert len(restored) == len(original)
    assert restored.fetched_at == 123.0
    assert StatIndex.from_json({"version": 0}) is None
    assert StatIndex.from_json(None) is None


# -- query building ---------------------------------------------------------------


def test_a_unique_is_searched_by_name_and_base():
    query = build_query(_unique("Tabula Rasa", "Simple Robe"), None)["query"]
    assert query["name"] == "Tabula Rasa"
    assert query["type"] == "Simple Robe"
    assert query["status"] == {"option": "online"}
    assert query["stats"] == [{"type": "and", "filters": []}]


def test_a_rare_is_searched_by_stat_ids_not_by_text():
    index = StatIndex.from_payload(price_payload("trade-stats.json"), 0.0)
    item = _rare("Two-Stone Ring", ["+58 to maximum Life", "18% increased Movement Speed"])
    query = build_query(item, index)["query"]
    filters = query["stats"][0]["filters"]
    assert filters, "no stat filters were built"
    assert all(f["id"].count(".") == 1 for f in filters)
    serialized = str(query)
    assert "maximum Life" not in serialized


def test_an_unknown_mod_is_dropped_rather_than_guessed():
    """A wrong filter returns nothing, which reads as 'worthless'. A missing one
    returns a superset, which is merely a wide answer."""
    index = StatIndex.from_payload(price_payload("trade-stats.json"), 0.0)
    item = _rare("Two-Stone Ring", ["+58 to maximum Life", "Grants Level 20 Nonexistent"])
    filters = build_query(item, index)["query"]["stats"][0]["filters"]
    assert len(filters) == 1


def test_corruption_is_a_filter():
    item = _unique("Tabula Rasa", "Simple Robe", corrupted=True)
    misc = build_query(item, None)["query"]["filters"]["misc_filters"]["filters"]
    assert misc["corrupted"] == {"option": "true"}


def test_the_sort_is_cheapest_first():
    assert build_query(_unique("Tabula Rasa", "Simple Robe"), None)["sort"] == {"price": "asc"}


# -- the median -------------------------------------------------------------------


def test_the_median_ignores_the_bottom_outlier():
    """The cheapest listing is usually a bot or a typo (SPEC §5.3)."""
    assert median_of_cheapest([1, 40, 42, 44, 46]) == 42
    assert median_of_cheapest([]) is None
    assert median_of_cheapest([0, 0]) is None
    assert median_of_cheapest([10]) == 10


# -- the whole quote --------------------------------------------------------------


@pytest.fixture
def trade(stack, tmp_path, cache_clock):
    from modules.net.backend.api import NetApi

    return TradeClient(
        stack.api(NetApi), Storage(tmp_path / "cache", "prices"), clock=cache_clock
    )


def _chaos_of(currency: str) -> float | None:
    return {"chaos": 1.0, "divine": 200.0}.get(currency)


async def test_a_quote_is_one_search_and_one_fetch(trade, server):
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    paths = [r.url.path for r in server.trade_requests()]
    assert len(paths) == 2
    assert paths[0] == "/api/trade/search/Standard"
    assert paths[1].startswith("/api/trade/fetch/")
    assert quote.total == 84
    assert trade.requests == 2


async def test_at_most_ten_ids_are_fetched(trade, server):
    """The fetch endpoint's own limit; asking for eleven is a 400."""
    await trade.quote(_unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of)
    fetch = next(r for r in server.trade_requests() if "/fetch/" in r.url.path)
    ids = fetch.url.path.rsplit("/", 1)[-1].split(",")
    assert len(ids) <= MAX_FETCH_IDS
    # The query id has to travel with the fetch or GGG rejects it.
    assert fetch.url.params.get("query") == "FIXTUREQID"


async def test_offline_sellers_are_excluded(trade):
    """The fixture alternates online and offline; only half may count."""
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    assert quote.considered == 10
    assert quote.online == 5
    assert len(quote.listings) == 5


async def test_the_quote_is_the_median_of_the_online_listings(trade):
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    # Online listings in the fixture are indices 0,2,4,6,8: 4c, 6c, 10c, 15c, 2 divine.
    assert quote.listings == [4.0, 6.0, 10.0, 15.0, 400.0]
    assert quote.chaos == 10.0
    # Not the minimum, which is the point.
    assert quote.chaos != min(quote.listings)


async def test_prices_in_other_currencies_are_converted(trade):
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    assert 400.0 in quote.listings, "the 2-divine listing was dropped or unconverted"


async def test_a_listing_in_a_currency_we_cannot_price_is_skipped(trade):
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=lambda _c: None
    )
    assert quote.online == 5
    assert quote.listings == []
    assert quote.chaos is None


async def test_the_quote_carries_a_link_back_to_the_site(trade):
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    assert quote.query_url == "https://www.pathofexile.com/trade/search/Standard/FIXTUREQID"


async def test_a_quote_is_anonymous(trade, server):
    """Trade search needs no credential, and sending one ties a public query to the
    account for nothing."""
    await trade.quote(_unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of)
    for request in server.trade_requests():
        assert "cookie" not in {k.lower() for k in request.headers}


async def test_a_rare_loads_the_stat_document_once(trade, server):
    item = _rare("Two-Stone Ring", ["+58 to maximum Life"])
    await trade.quote(item, "Standard", chaos_of=_chaos_of)
    await trade.quote(item, "Standard", chaos_of=_chaos_of)
    stats = [r for r in server.trade_requests() if r.url.path.endswith("/data/stats")]
    assert len(stats) == 1


async def test_the_stat_document_is_cached_across_instances(trade, stack, tmp_path, cache_clock):
    from modules.net.backend.api import NetApi

    await trade.stats()
    second = TradeClient(
        stack.api(NetApi), Storage(tmp_path / "cache", "prices"), clock=cache_clock
    )
    await second.stats()
    assert second.requests == 0, "a fresh client refetched a cached document"


async def test_the_trade_buckets_are_separate_from_the_item_bucket(trade, stack):
    from modules.net.backend.api import NetApi
    from modules.poeapi.backend.api import PoeApi

    await stack.api(PoeApi).get_items(refresh=True)
    await trade.quote(_unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of)
    policies = {snap.policy for snap in stack.api(NetApi).limits()}
    # All three learned from real response headers, and all three distinct.
    assert {
        "backend-item-request-limit",
        "trade-search-request-limit",
        "trade-fetch-request-limit",
    } <= policies


async def test_a_search_with_no_results_is_a_quote_of_none(trade, server, monkeypatch):
    import tests.conftest as conftest

    real = conftest.price_payload

    def empty(name: str):
        if name == "trade-search.json":
            return {"id": "X", "result": [], "total": 0}
        return real(name)

    monkeypatch.setattr(conftest, "price_payload", empty)
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    assert quote.chaos is None
    assert quote.total == 0
    assert len(server.trade_requests()) == 1, "an empty search still fetched"


async def test_the_quote_is_json_serializable(trade):
    quote = await trade.quote(
        _unique("Tabula Rasa", "Simple Robe"), "Standard", chaos_of=_chaos_of
    )
    import json

    assert json.loads(json.dumps(quote.to_json()))["chaos"] == 10.0


# -- through the module ------------------------------------------------------------


async def test_the_module_quotes_on_demand_and_only_then(priced, priced_stack, server):
    from modules.poeapi.backend.api import PoeApi

    bag = await priced_stack.api(PoeApi).get_items()
    tabula = next(i for i in bag.items if i.name == "Tabula Rasa")
    assert server.trade_requests() == []
    quote = await priced.quote(tabula)
    assert quote.chaos is not None
    assert len(server.trade_requests()) == 2


async def test_the_module_converts_listing_prices_with_its_own_tables(priced, priced_stack):
    from modules.poeapi.backend.api import PoeApi

    bag = await priced_stack.api(PoeApi).get_items()
    tabula = next(i for i in bag.items if i.name == "Tabula Rasa")
    quote = await priced.quote(tabula)
    # The 2-divine listing converts at the fixture table's rate, not at a constant.
    index = priced.index()
    assert max(quote.listings) == pytest.approx(2 * index.chaos_per_divine)


async def test_quote_by_uid_refuses_an_item_that_is_not_in_the_bag(prices_module, priced_stack):
    from modules.prices.backend.api import PricesError

    with pytest.raises(PricesError, match="no item"):
        await prices_module.methods()["quote"]("not-a-real-uid")


# -- helpers ------------------------------------------------------------------------


def _unique(name: str, base: str, *, corrupted: bool = False) -> NormalizedItem:
    return NormalizedItem(
        uid="u",
        name=name,
        base_type=base,
        category="armour",
        rarity=Rarity.UNIQUE,
        corrupted=corrupted,
        location=Location(source=Source.BAG),
    )


def _rare(base: str, mods: list[str]) -> NormalizedItem:
    from modules.poeapi.backend.api import Mods

    return NormalizedItem(
        uid="r",
        name="Corpse Loop",
        base_type=base,
        category="accessory",
        rarity=Rarity.RARE,
        mods=Mods(explicit=mods),
        location=Location(source=Source.BAG),
    )


def test_the_fake_clock_is_used_not_the_wall_clock():
    """Guards the fixtures above: a real clock would make the TTL tests time bombs."""
    clock = FakeClock(5.0)
    assert clock() == 5.0


# -- bug 1: the query was an exact-match conjunction of every mod -------------------
#
# The first live appraisal escalated three gate-flagged rares and got two zeroes and
# one single-listing "median" out of it. `build_query` ANDed every resolvable
# explicit and implicit mod into one filter set, so a six-mod rare asked the market
# for a near-duplicate of itself. Raising the eager timeout from 12s to 60s changed
# nothing, which is what ruled out slowness and left query shape.

# The live ring, mod for mod. Seven filters went out; zero listings came back.
SIX_MOD_RARE = [
    "+30 to Strength",
    "Adds 2 to 5 Physical Damage to Attacks",
    "Adds 1 to 28 Lightning Damage to Attacks",
    "+103 to maximum Life",
    "+33% to Fire Resistance",
    "+38% to Lightning Resistance",
]


def _stats() -> StatIndex:
    """An index in which **every** one of the six mods resolves.

    Deliberately not the shared ``trade-stats.json`` fixture, which happens to carry
    only one of them: this test is about what the builder does when it *could* name
    every mod, and against that fixture it would pass for the wrong reason. These are
    the real opaque ids, resolved against the live stats document during the
    investigation and pasted here so the test needs no network.
    """
    return StatIndex(
        {
            normalize_stat_text("+30 to Strength"): "explicit.stat_4080418644",
            normalize_stat_text(
                "Adds 2 to 5 Physical Damage to Attacks"
            ): "pseudo.pseudo_adds_physical_damage_to_attacks",
            normalize_stat_text(
                "Adds 1 to 28 Lightning Damage to Attacks"
            ): "pseudo.pseudo_adds_lightning_damage_to_attacks",
            normalize_stat_text("+103 to maximum Life"): "explicit.stat_3299347043",
            normalize_stat_text("+33% to Fire Resistance"): "explicit.stat_3372524247",
            normalize_stat_text("+38% to Lightning Resistance"): "explicit.stat_1671376347",
        },
        0.0,
    )


def test_bug1_a_six_mod_rare_is_not_an_exact_match_conjunction_of_all_its_mods():
    """The regression, stated as the property that was violated.

    Every one of these six mods resolves to a stat id, so the old builder emitted six
    ANDed filters. The rule is not "fewer filters is nicer" — it is that a query
    naming every mod on the item can only match near-duplicates of it, and a price
    taken from near-duplicates of a random rare is a price taken from nothing.
    """
    item = _rare("Amethyst Ring", SIX_MOD_RARE)
    filters = build_query(item, _stats())["query"]["stats"][0]["filters"]

    assert len(filters) < len(SIX_MOD_RARE), "every mod is still in the query"
    assert len(filters) <= MAX_STAT_FILTERS


def test_bug1_the_caller_chooses_which_mods_the_query_is_about():
    """`prices` cannot ask the gate — that would be a dependency cycle — so the
    caller that decided to escalate says why, and the query is built from that."""
    item = _rare("Amethyst Ring", SIX_MOD_RARE)
    plan = build_plan(
        item,
        _stats(),
        [ModFocus(text="+103 to maximum Life", minimum=82, label="max life")],
    )
    filters = plan[0].body["query"]["stats"][0]["filters"]
    assert len(filters) == 1
    assert filters[0]["value"] == {"min": 82}
    # ...and it is legible, because a tier-3 number nobody can trace is a number
    # nobody can argue with.
    assert "max life ≥ 82" in plan[0].description
    assert "Amethyst Ring" in plan[0].description


def test_bug1_a_roll_becomes_a_widened_range_not_an_equality():
    """Trade filters take min/max. Matching the exact roll of a random rare is the
    same mistake as naming every mod, in one dimension instead of six."""
    assert widened(103) == 82
    assert widened(109) == 87
    # Small rolls stay meaningful rather than collapsing to zero.
    assert widened(0.34) == 0.27

    item = _rare("Amethyst Ring", SIX_MOD_RARE)
    focus = [ModFocus(text="+103 to maximum Life", minimum=widened(103), label="max life")]
    entry = build_query(item, _stats(), focus)["query"]["stats"][0]["filters"][0]
    assert entry["value"]["min"] < 103


def test_bug1_a_rare_is_priced_against_rares():
    """Left open, a base-type query lets a unique of the same base — priced by its
    unique name, not by its mods — into the sample."""
    item = _rare("Amethyst Ring", SIX_MOD_RARE)
    filters = build_query(item, _stats())["query"]["filters"]["type_filters"]["filters"]
    assert filters["rarity"] == {"option": "rare"}
    # A unique is still searched by name, where rarity is implied and would only
    # narrow the query for nothing.
    assert "filters" not in build_query(_unique("Tabula Rasa", "Simple Robe"), None)["query"]


def test_bug1_the_plan_has_a_broadening_step_that_is_strictly_wider():
    item = _rare("Amethyst Ring", SIX_MOD_RARE)
    focus = [ModFocus(text="+103 to maximum Life", minimum=82, label="max life")]
    plan = build_plan(item, _stats(), focus)
    assert len(plan) == 2
    first, second = plan
    assert first.body["query"]["stats"][0]["filters"][0]["value"] == {"min": 82}
    # Same mod, no floor: the smallest change that can turn a zero into a number.
    assert "value" not in second.body["query"]["stats"][0]["filters"][0]
    assert "broadened" in second.description


def test_bug1_a_mod_nobody_can_resolve_never_becomes_a_bare_base_type_search_silently():
    """It is still made — for an item with no readable mods the base type genuinely
    is everything we know — but it says so, so a surprising number is traceable."""
    item = _rare("Amethyst Ring", ["Grants Level 20 Nonexistent"])
    plan = build_plan(item, _stats())
    assert plan[0].body["query"]["stats"][0]["filters"] == []
    assert "base type only" in plan[0].description


async def test_bug1_a_zero_result_search_is_retried_once_wider_and_never_twice(
    trade, server, monkeypatch
):
    """One extra request, for items that would otherwise have no answer at all."""
    import tests.conftest as conftest

    real = conftest.price_payload

    def empty(name: str):
        if name == "trade-search.json":
            return {"id": "X", "result": [], "total": 0}
        return real(name)

    monkeypatch.setattr(conftest, "price_payload", empty)
    item = _rare("Amethyst Ring", SIX_MOD_RARE)
    quote = await trade.quote(
        item,
        "Standard",
        chaos_of=_chaos_of,
        focus=[ModFocus(text="+58 to maximum Life", minimum=46, label="max life")],
    )

    searches = [r for r in server.trade_requests() if "/search/" in r.url.path]
    assert len(searches) == 2, "the broadening retry did not run, or ran more than once"
    assert quote.attempts == 2
    assert quote.chaos is None
    # An *answer*, not an absence: the caller has to be able to tell this from a
    # query that never finished. See bug 2.
    assert quote.searched
    assert quote.total == 0


async def test_bug1_a_search_that_works_first_time_costs_no_extra_request(trade, server):
    item = _rare("Amethyst Ring", ["+58 to maximum Life"])
    await trade.quote(item, "Standard", chaos_of=_chaos_of)
    searches = [r for r in server.trade_requests() if "/search/" in r.url.path]
    assert len(searches) == 1


async def test_bug1_the_quote_carries_the_query_that_produced_it(trade):
    """Bug 1's other half: the live run reported `10.0c · trade search` for a jewel
    with exactly one comparable in the league, and nothing on screen could have told
    anybody that. The quote now carries the filters and the match count."""
    item = _rare("Amethyst Ring", ["+58 to maximum Life"])
    quote = await trade.quote(item, "Standard", chaos_of=_chaos_of)
    assert quote.query
    assert "Amethyst Ring" in quote.query
    assert quote.to_json()["query"] == quote.query
    assert quote.to_json()["attempts"] == quote.attempts
