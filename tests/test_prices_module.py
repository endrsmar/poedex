"""The `prices` module in a started registry: prefetch, ETags, budgets, lifecycle.

Everything runs through the same ``httpx.MockTransport`` the Phase 2 stack uses, so
the requests under assertion are the requests the product would send — same client,
same limiter, same credential path.
"""

from __future__ import annotations

import asyncio

import pytest

from modules.net.backend.api import NetApi
from modules.poeapi.backend.api import PoeApi, Source
from modules.prices.backend.api import (
    PRICES_UPDATED,
    PricesApi,
    PricesError,
    PriceSource,
)
from modules.prices.backend.module import PricesModule
from modules.prices.backend.ninja import PREFETCH
from runtime.errors import KindViolationError, ModuleNotStartedError

NINJA = "poe.ninja"
GGG = "www.pathofexile.com"


# -- the declaration --------------------------------------------------------------


def test_it_is_a_feature_module_requiring_poeapi():
    module = PricesModule()
    assert module.kind == "feature"
    assert "poeapi" in module.requires
    # `net` too, and deliberately: poe.ninja and the trade API are not account
    # endpoints, so `poeapi` neither fetches nor should fetch them, and plan §4
    # forbids a module opening its own socket. See the module's own note.
    assert "net" in module.requires
    assert module.provides is PricesApi


async def test_a_core_module_cannot_require_it(registry, fake_module):
    registry.register(PricesModule(prefetch=False))
    registry.register(fake_module("plumbing", kind="core", requires=["prices"]))
    with pytest.raises(KindViolationError):
        registry.resolve()


async def test_it_refuses_to_work_before_it_starts():
    module = PricesModule(prefetch=False)
    with pytest.raises(ModuleNotStartedError):
        await module.bulk("currency")


# -- the prefetch -----------------------------------------------------------------


async def test_start_prefetches_every_wanted_table(priced_stack, server, prices_module):
    fetched = [r for r in server.to_host(NINJA)]
    assert len(fetched) == len(PREFETCH)
    status = prices_module.status()
    assert status.loaded == len(PREFETCH) == status.requested
    assert status.healthy


async def test_the_prefetch_runs_in_the_background_at_start(stack_factory, server, cache_clock):
    """Module start must not block on somebody else's web server."""
    module = PricesModule(clock=cache_clock, prefetch=True)
    await stack_factory(module)
    assert module.status().loaded == 0, "start awaited the prefetch"
    for _ in range(20):
        await asyncio.sleep(0)
        if module.status().loaded == len(PREFETCH):
            break
    assert module.status().loaded == len(PREFETCH)


async def test_a_failing_table_does_not_cost_the_others(
    stack_factory, server, cache_clock, clock
):
    server.ninja_status = 500
    module = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(module)
    status = await module.refresh()
    assert status.loaded == 0
    assert status.note and "HTTP 500" in status.note
    # And it recovers without a restart. A 500 puts the host policy in backoff, so
    # this waits it out the way the limiter would make us.
    server.ninja_status = 200
    clock.advance(600)
    status = await module.refresh()
    assert status.loaded == len(PREFETCH)
    assert status.note is None


async def test_a_refresh_emits_an_event(priced_stack, prices_module, server, clock):
    seen = []
    priced_stack.events.subscribe(PRICES_UPDATED, seen.append)
    server.etag = "W/fixture-2"
    clock.advance(61)
    await prices_module.refresh(force=True)
    assert seen and seen[0].payload["loaded"] == len(PREFETCH)
    assert seen[0].payload["changed"] == len(PREFETCH)


# -- ETags and expiry ---------------------------------------------------------------


async def test_a_fresh_table_is_not_re_fetched(priced_stack, prices_module, server):
    """Thirty minutes is the server's own max-age. Inside it, no request at all."""
    before = len(server.to_host(NINJA))
    await prices_module.refresh()
    assert len(server.to_host(NINJA)) == before


async def test_expiry_triggers_a_conditional_request_that_can_come_back_304(
    priced_stack, prices_module, server, cache_clock, clock
):
    before = len(server.to_host(NINJA))
    cache_clock.advance(1801)
    clock.advance(1801)
    await prices_module.refresh()

    conditional = server.to_host(NINJA)[before:]
    assert len(conditional) == len(PREFETCH)
    assert all(r.headers.get("if-none-match") == server.etag for r in conditional)
    # 304, so the tables are unchanged but their age resets: the *check* is fresh.
    assert prices_module.status().loaded == len(PREFETCH)
    assert prices_module.status().newest is not None
    assert not prices_module.status().stale


async def test_a_changed_etag_replaces_the_table(
    priced_stack, prices_module, server, cache_clock, clock
):
    currency = prices_module.index().table("currency")
    assert currency is not None and currency.etag == "W/fixture-1"
    server.etag = "W/fixture-2"
    cache_clock.advance(1801)
    clock.advance(1801)
    await prices_module.refresh()
    assert prices_module.index().table("currency").etag == "W/fixture-2"


async def test_force_ignores_both_the_ttl_and_the_etag(
    priced_stack, prices_module, server, clock
):
    before = len(server.to_host(NINJA))
    clock.advance(61)
    await prices_module.refresh(force=True)
    forced = server.to_host(NINJA)[before:]
    assert len(forced) == len(PREFETCH)
    assert all("if-none-match" not in r.headers for r in forced)


async def test_a_long_outage_is_reported_as_stale_rather_than_hidden(
    priced_stack, prices_module, cache_clock, clock, server
):
    server.ninja_status = 503
    cache_clock.advance(4000)
    clock.advance(4000)
    status = await prices_module.refresh()
    assert status.loaded == len(PREFETCH), "the cached tables are still there"
    assert status.stale
    assert status.note and "503" in status.note


async def test_tables_survive_a_restart(stack_factory, registry, server, cache_clock):
    """A warm start prices the bag before a single request goes out."""
    first = PricesModule(clock=cache_clock, prefetch=False)
    await stack_factory(first)
    await first.refresh()
    await registry.stop_all()
    ninja_requests = len(server.to_host(NINJA))

    second = PricesModule(clock=cache_clock, prefetch=False)
    # A second registry over the same storage root is what a restart looks like.
    from runtime.registry import Registry

    fresh = Registry(
        events=registry.events,
        storage=registry.storage,
        settings=registry.settings,
    )
    for module in (registry.get("credentials"), registry.get("net"), registry.get("poeapi")):
        fresh.register(module)
    fresh.register(second)
    await fresh.start_all()
    try:
        assert second.status().loaded == len(PREFETCH)
        assert len(server.to_host(NINJA)) == ninja_requests, "a warm start refetched"
    finally:
        await fresh.stop_all()


# -- budgets ------------------------------------------------------------------------


async def test_poe_ninja_does_not_touch_the_ggg_buckets(priced_stack, server):
    """poe.ninja is a different host. Pricing a bag must never delay a sync."""
    net = priced_stack.api(NetApi)
    policies = {snap.policy for snap in net.limits()}
    assert "host:poe.ninja" in policies
    ninja = [s for s in net.limits() if s.policy == "host:poe.ninja"]
    assert len(ninja) == 1
    assert ninja[0].hits == len(PREFETCH)
    # Nothing GGG-shaped has been touched by the prefetch.
    assert not any(p.startswith("backend-") for p in policies if p != "host:poe.ninja")


async def test_the_ggg_and_ninja_buckets_stay_separate_once_both_are_used(priced_stack, server):
    net = priced_stack.api(NetApi)
    await priced_stack.api(PoeApi).get_items(refresh=True)
    by_policy = {}
    for snap in net.limits():
        by_policy.setdefault(snap.policy, []).append(snap)
    assert "backend-item-request-limit" in by_policy
    assert "host:poe.ninja" in by_policy
    assert by_policy["host:poe.ninja"][0].hits == len(PREFETCH)
    assert all(s.hits <= 2 for s in by_policy["backend-item-request-limit"])


async def test_the_credential_never_reaches_poe_ninja(priced_stack, server):
    """POESESSID is a full-account website credential (SPEC §8)."""
    for request in server.to_host(NINJA):
        assert "cookie" not in {k.lower() for k in request.headers}
    # And it does reach the host it belongs to.
    await priced_stack.api(PoeApi).get_items(refresh=True)
    ggg = server.to_host(GGG)
    assert ggg and any("cookie" in {k.lower() for k in r.headers} for r in ggg)


async def test_the_requests_go_to_the_measured_routes(priced_stack, server):
    paths = {r.url.path for r in server.to_host(NINJA)}
    assert paths == {
        "/poe1/api/economy/exchange/current/overview",
        "/poe1/api/economy/stash/current/item/overview",
    }
    types = {r.url.params.get("type") for r in server.to_host(NINJA)}
    assert "Currency" in types and "UniqueWeapon" in types
    assert all(r.url.params.get("league") == "Standard" for r in server.to_host(NINJA))


# -- valuation through the module ------------------------------------------------------


async def test_valuing_the_bag_makes_no_request_at_all(priced_stack, priced, server):
    bag = await priced_stack.api(PoeApi).get_items()
    before = len(server.requests)
    result = await priced.value_all(bag.by_source(Source.BAG))
    assert len(server.requests) == before
    assert result.total_chaos > 0
    assert result.trade_requests == 0


async def test_a_valuation_pass_issues_zero_trade_requests(priced_stack, priced, server):
    """The counter *and* the wire, because either alone could be lying."""
    bag = await priced_stack.api(PoeApi).get_items()
    await priced.value_all(bag.by_source(Source.BAG))
    await priced.value(bag.by_source(Source.BAG)[0])
    assert server.trade_requests() == []


async def test_the_bag_totals_are_believable(priced_stack, priced):
    bag = await priced_stack.api(PoeApi).get_items()
    result = await priced.value_all(bag.by_source(Source.BAG))
    names = {v.name for v in result.priced}
    assert {"Chaos Orb", "Divine Orb", "Tabula Rasa", "Headhunter"} <= names
    assert {v.name for v in result.unpriceable} >= {"Veiled Scarab"}
    assert result.divine_rate and result.total_divine
    assert result.table is not None and result.table.loaded == len(PREFETCH)
    # Worn gear is not in the bag and must not be in the total.
    assert len(result.items) == len(bag.by_source(Source.BAG))


async def test_the_json_method_surface_works(priced_stack, prices_module):
    methods = prices_module.methods()
    assert set(methods) == {"value_bag", "status", "refresh", "quote"}
    payload = await methods["value_bag"]()
    assert payload["priced_count"] > 0
    assert payload["trade_requests"] == 0
    assert (await methods["status"]())["loaded"] == len(PREFETCH)


# -- bulk() -----------------------------------------------------------------------------


async def test_bulk_returns_a_name_keyed_table(priced, priced_stack):
    table = await priced.bulk("currency")
    assert table["Divine Orb"].chaos > 1
    assert table["Divine Orb"].source is PriceSource.BULK
    assert table["Divine Orb"].as_of is not None


async def test_bulk_of_an_unknown_category_is_an_error(priced):
    with pytest.raises(PricesError, match="unknown price category"):
        await priced.bulk("wombats")


async def test_bulk_fetches_a_category_that_was_not_prefetched(priced, server):
    """The one path that may fetch outside a refresh, and only when asked by name."""
    before = len(server.to_host(NINJA))
    assert "resonator" not in PREFETCH
    await priced.bulk("resonator")
    # The fixture server does not carry a Resonator table, so this 404s — the
    # assertion is that it was *attempted*, not that it succeeded.
    attempted = server.to_host(NINJA)[before:]
    assert [r.url.params.get("type") for r in attempted] == ["Resonator"]


# -- lifecycle -----------------------------------------------------------------------------


async def test_stop_cancels_an_in_flight_prefetch(stack_factory, registry, cache_clock):
    module = PricesModule(clock=cache_clock, prefetch=True)
    await stack_factory(module)
    await registry.stop_all()
    assert module._prefetch_task is None


def test_repr_says_what_it_holds():
    assert "tables=0" in repr(PricesModule(prefetch=False))
