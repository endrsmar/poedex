"""Tier 1b — GGG's bulk exchange, for currency poe.ninja does not index.

This is the safety net, not the ducat fix. Ducats came back unpriceable because
``CATALOGUE`` had never heard of the ``Ducat`` type, and `test_prices_discovery`
covers the actual repair. What is here is for the case discovery cannot reach: a
tradeable currency poe.ninja has no table for at all.

The awkward part is not the endpoint, it is the batching. The response is capped at
a hundred rows sorted cheapest-first **across the whole batch**, so ten ids in one
request come back as everybody's floor and nobody's median. Half this file is about
that.
"""

from __future__ import annotations

import pytest

from modules.poeapi.backend.api import Location, NormalizedItem, Rarity, Source
from modules.prices.backend.api import PriceSource
from modules.prices.backend.exchange import (
    EXCHANGE_MAX_WANTS,
    ExchangeClient,
    StaticIndex,
    parse_exchange_offers,
)
from runtime.storage import Storage
from tests.conftest import price_payload

DUCATS = [
    "Merrick's Ducat",
    "Ukatoa's Ducat",
    "Cyaxan's Ducat",
    "Kishara's Ducat",
    "Telesia's Ducat",
]


def _currency(name: str, *, stack: int = 1) -> NormalizedItem:
    return NormalizedItem(
        uid=f"uid-{name}",
        name=name,
        base_type=name,
        category="currency",
        rarity=Rarity.CURRENCY,
        stack_size=stack,
        location=Location(source=Source.BAG),
    )


@pytest.fixture
def exchange(stack, tmp_path, cache_clock):
    from modules.net.backend.api import NetApi

    return ExchangeClient(
        stack.api(NetApi), Storage(tmp_path / "cache", "prices"), clock=cache_clock
    )


# -- reading the wire ------------------------------------------------------------


def test_the_rate_is_what_you_pay_divided_by_what_you_get():
    """The wire says "N chaos for M ducats". Nothing on it is a unit price."""
    rates, returned, total = parse_exchange_offers(price_payload("trade-exchange.json"))
    assert returned == total > 0
    assert set(rates) == {"merricks-ducat", "ukatoas-ducat"}
    assert all(value > 0 for values in rates.values() for value in values)
    assert rates["merricks-ducat"] == sorted(rates["merricks-ducat"])


def test_offline_sellers_and_non_chaos_offers_are_skipped():
    """An offer priced in divine needs a divine rate, and we are here precisely
    because the tables that carry one did not have this item."""
    payload = price_payload("trade-exchange.json")
    rates, _returned, _total = parse_exchange_offers(payload)
    online_chaos = 0
    for row in payload["result"].values():
        listing = row["listing"]
        for offer in listing["offers"]:
            if "online" in listing["account"] and offer["exchange"]["currency"] == "chaos":
                online_chaos += 1
    assert sum(len(v) for v in rates.values()) == online_chaos
    assert online_chaos < len(payload["result"]), "the fixture has no excluded rows"


def test_the_static_document_maps_a_name_to_an_id():
    index = StaticIndex.from_payload(price_payload("trade-static.json"), 0.0)
    assert index.trade_id("Merrick's Ducat") == "merricks-ducat"
    assert index.trade_id("merrick's ducat") == "merricks-ducat"
    assert index.trade_id("Chaos Orb") == "chaos"
    assert index.trade_id("A Thing That Does Not Exist") is None


# -- the median ------------------------------------------------------------------


async def test_the_price_is_the_median_of_the_cheapest_not_the_minimum(exchange):
    """The two cheapest ducat offers on the live wire were 1 chaos with stock 1.
    That is somebody dumping a single unit, not a rate."""
    rates = await exchange.rates(["Merrick's Ducat"], "Allflame")
    rate = rates["Merrick's Ducat"]
    assert rate.chaos == 3.0
    assert rate.offers > 10
    assert rate.chaos > 1.0, "the minimum leaked into the answer"


# -- batching --------------------------------------------------------------------


async def test_five_currencies_cost_one_request(exchange, server):
    """The whole reason batching exists: the endpoint takes an array."""
    await exchange.rates(DUCATS, "Allflame")
    posts = [r for r in server.trade_requests() if "/exchange/" in r.url.path]
    assert len(posts) == 1 < len(DUCATS)
    assert server.exchange_wants == [
        ["merricks-ducat", "ukatoas-ducat", "cyaxans-ducat", "kisharas-ducat",
         "telesias-ducat"]
    ]


async def test_a_batch_never_asks_for_more_than_the_endpoint_allows(exchange, server):
    """Eleven is a 400 with GGG's own wording. Twelve names is two requests."""
    names = [
        *DUCATS,
        "Rotmother's Ducat", "Brinehook's Ducat", "Katakohi's Ducat",
        "Tzamoto's Ducat", "The Genteel's Ducat", "The Changeling's Ducat",
        "Chaos Orb",
    ]
    await exchange.rates(names, "Allflame")
    assert all(len(want) <= EXCHANGE_MAX_WANTS for want in server.exchange_wants)
    assert len(server.exchange_wants) == 2


async def test_a_starved_want_is_re_queried_rather_than_priced_off_the_floor(
    exchange, server
):
    """The measured trap, reproduced.

    With the response capped, a cheapest-first sort across the batch gives the
    popular currency all the rows and the thin one two offers at the floor. A want
    that came back with at least ``sample`` offers holds its own cheapest ``sample``
    and is trusted; one that did not is asked again on its own.
    """
    server.exchange_cap = 12
    rates = await exchange.rates(["Merrick's Ducat", "Ukatoa's Ducat"], "Allflame",
                                 sample=10, max_requeries=4)
    assert len(server.exchange_wants) == 3, server.exchange_wants
    assert server.exchange_wants[0] == ["merricks-ducat", "ukatoas-ducat"]
    assert [len(w) for w in server.exchange_wants[1:]] == [1, 1]
    # ...and the re-query is what makes the number right.
    assert rates["Merrick's Ducat"].chaos == 3.0
    assert not rates["Merrick's Ducat"].truncated


async def test_the_re_query_budget_is_a_cap_and_the_rest_say_they_are_a_floor(
    exchange, server
):
    server.exchange_cap = 12
    rates = await exchange.rates(["Merrick's Ducat", "Ukatoa's Ducat"], "Allflame",
                                 sample=10, max_requeries=1)
    assert len(server.exchange_wants) == 2
    truncated = [name for name, rate in rates.items() if rate.truncated]
    assert truncated, "a starved want was silently reported as a clean median"


# -- caching ---------------------------------------------------------------------


async def test_a_second_lookup_of_the_same_name_costs_nothing(exchange, server):
    await exchange.rates(["Merrick's Ducat"], "Allflame")
    before = len(server.exchange_wants)
    again = await exchange.rates(["Merrick's Ducat"], "Allflame")
    assert len(server.exchange_wants) == before
    assert again["Merrick's Ducat"].chaos == 3.0


async def test_rates_are_per_league_and_never_reused_across_them(exchange, server):
    """A Merrick's Ducat is 3c in Allflame and does not trade at all in Standard.

    The same rule `prices` applies to poe.ninja's tables applies here, and for a
    stronger reason: these are live offers in one economy.
    """
    await exchange.rates(["Merrick's Ducat"], "Allflame")
    before = len(server.exchange_wants)
    await exchange.rates(["Merrick's Ducat"], "Standard")
    assert len(server.exchange_wants) == before + 1
    leagues = [r.url.path.rsplit("/", 1)[-1] for r in server.trade_requests()
               if "/exchange/" in r.url.path]
    assert leagues == ["Allflame", "Standard"]
    assert exchange.cached("Allflame").rates.keys() == {"merricks-ducat"}
    assert exchange.cached("Standard").rates.keys() == {"merricks-ducat"}


async def test_the_cache_survives_a_new_client_over_the_same_storage(
    exchange, stack, tmp_path, cache_clock, server
):
    from modules.net.backend.api import NetApi

    await exchange.rates(["Merrick's Ducat"], "Allflame")
    second = ExchangeClient(
        stack.api(NetApi), Storage(tmp_path / "cache", "prices"), clock=cache_clock
    )
    rates = await second.rates(["Merrick's Ducat"], "Allflame")
    assert rates["Merrick's Ducat"].chaos == 3.0
    assert second.requests == 0, "a fresh client refetched a cached rate"


async def test_an_expired_cache_is_asked_again(exchange, server, cache_clock):
    await exchange.rates(["Merrick's Ducat"], "Allflame")
    before = len(server.exchange_wants)
    cache_clock.advance(21601)
    await exchange.rates(["Merrick's Ducat"], "Allflame")
    assert len(server.exchange_wants) == before + 1


# -- through the module ------------------------------------------------------------


async def test_a_currency_missing_from_bulk_is_priced_and_labelled_as_exchange(priced):
    """The gap this tier exists for, end to end.

    ``Ducat`` is not one of the tables the fixture server serves, so tier 1 misses
    it entirely — and the answer still comes back with a number, a source that says
    where it came from, and a stack-aware total.
    """
    result = await priced.value_all([_currency("Merrick's Ducat", stack=7)])
    row = result.items[0]
    assert not row.unpriceable
    assert row.source is PriceSource.EXCHANGE
    assert row.price is not None and row.price.chaos == 3.0
    assert row.total_chaos == pytest.approx(21.0)
    assert row.price.detail and "live offer" in row.price.detail
    assert result.exchange_requests >= 1


async def test_the_fallback_is_only_asked_about_what_bulk_missed(priced, server):
    """A Chaos Orb is in the currency table. Asking the exchange about it would be a
    request spent to learn something we already knew."""
    result = await priced.value_all([_currency("Chaos Orb", stack=3),
                                     _currency("Merrick's Ducat")])
    assert result.items[0].source is PriceSource.BULK
    assert server.exchange_wants == [["merricks-ducat"]]


async def test_a_currency_nobody_is_selling_stays_unpriceable(priced):
    """Absence is the answer, and the answer is not zero."""
    result = await priced.value_all([_currency("Cyaxan's Ducat", stack=4)])
    row = result.items[0]
    assert row.unpriceable
    assert row.total_chaos == 0.0
    assert row.reason and "bulk-exchange" in row.reason
    assert result.unpriceable_stack == 4


async def test_a_rare_is_never_sent_to_the_bulk_exchange(priced, server):
    """It deals in stackable currency. Sending it a rare would be tier 3's job done
    badly, and would spend a tight budget doing it."""
    rare = NormalizedItem(
        uid="r", name="Corpse Loop", base_type="Two-Stone Ring",
        category="accessory", rarity=Rarity.RARE, location=Location(source=Source.BAG),
    )
    result = await priced.value_all([rare])
    assert server.exchange_wants == []
    assert result.items[0].unpriceable
    assert result.exchange_requests == 0


async def test_the_fallback_can_be_switched_off_for_one_call(priced, server):
    result = await priced.value_all([_currency("Merrick's Ducat")], exchange=False)
    assert server.exchange_wants == []
    assert result.items[0].unpriceable
    assert result.exchange_requests == 0
    # ...and the reason does not claim we asked.
    assert result.items[0].reason == "not in the poe.ninja index for this league"
