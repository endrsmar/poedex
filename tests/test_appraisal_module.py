"""`appraisal` as a module: wiring, settings, methods, and the request budget.

The whole stack is real here — `credentials`, `net`, `poeapi`, `prices`, `appraisal`
— with an ``httpx.MockTransport`` where the socket would be. That matters for the one
claim this file exists to prove: appraising a bag issues **zero** trade requests, and
the way to know that is to count what reached the wire, not to read the code.
"""

from __future__ import annotations

import pytest

from modules.appraisal.backend.api import (
    APPRAISAL_COMPLETE,
    DEFAULT_KEEP_CHAOS,
    AppraisalApi,
    AppraisalError,
    Strictness,
    Verdict,
)
from modules.appraisal.backend.module import AppraisalModule
from modules.poeapi.backend.api import PoeApi, Source
from modules.prices.backend.api import PricesApi, PriceSource
from runtime.errors import ModuleNotStartedError

# -- declaration and wiring ----------------------------------------------------


def test_the_declaration_matches_the_plan_except_where_it_says_why():
    module = AppraisalModule()
    assert (module.id, module.kind) == ("appraisal", "feature")
    assert module.provides is AppraisalApi
    # `prices` is the plan's dependency. `poeapi` is the documented addition, and
    # this assertion is here so removing it is a deliberate act.
    assert set(module.requires) == {"prices", "poeapi"}
    assert isinstance(module, AppraisalApi)


def test_it_resolves_both_dependencies_at_start(appraised_stack):
    module = appraised_stack.get("appraisal")
    assert isinstance(module._prices, PricesApi)
    assert isinstance(module._poeapi, PoeApi)


async def test_an_unstarted_module_refuses_rather_than_returning_nonsense():
    module = AppraisalModule()
    with pytest.raises(ModuleNotStartedError):
        await module.appraise([])


async def test_stopping_releases_the_dependencies(appraised_stack, loot):
    module = appraised_stack.get("appraisal")
    await module.stop()
    with pytest.raises(ModuleNotStartedError):
        await module.appraise(loot)


def test_the_methods_are_namespaced_and_awaitable(appraised_stack):
    names = appraised_stack.methods.for_module("appraisal")
    assert names == ["appraisal.appraise_bag", "appraisal.gate", "appraisal.settings"]


# -- settings ------------------------------------------------------------------


def test_the_keep_threshold_is_a_setting_with_a_stated_default(appraised_stack):
    module = appraised_stack.get("appraisal")
    assert module.threshold() == DEFAULT_KEEP_CHAOS == 20.0
    assert module.settings_schema()["keep_threshold_chaos"]["default"] == DEFAULT_KEEP_CHAOS


async def test_changing_the_setting_changes_the_verdicts(appraised_stack, loot):
    module = appraised_stack.get("appraisal")
    appraised_stack.settings.set("appraisal", "keep_threshold_chaos", 1000.0)
    quiet = await module.appraise(loot)
    appraised_stack.settings.set("appraisal", "keep_threshold_chaos", 1.0)
    busy = await module.appraise(loot)
    assert quiet.counts["keep"] < busy.counts["keep"]
    assert quiet.threshold_chaos == 1000.0 and busy.threshold_chaos == 1.0
    # ...and the one state a threshold must not touch, at both ends.
    assert quiet.counts["unpriceable"] == busy.counts["unpriceable"] > 0


async def test_an_explicit_threshold_overrides_the_setting_for_one_call(appraised_stack, loot):
    module = appraised_stack.get("appraisal")
    result = await module.appraise(loot, threshold_chaos=5.0)
    assert result.threshold_chaos == 5.0
    assert module.threshold() == DEFAULT_KEEP_CHAOS, "the setting must not be mutated"


async def test_the_check_floor_can_never_exceed_the_keep_threshold(appraised_stack, loot):
    """Otherwise there is a band in which an item is simultaneously above the check
    floor and below the keep threshold *and* below the check floor — a contradiction
    that would resolve to whichever branch happened to be written first."""
    module = appraised_stack.get("appraisal")
    appraised_stack.settings.set("appraisal", "check_threshold_chaos", 500.0)
    result = await module.appraise(loot, threshold_chaos=10.0)
    assert result.counts["check"] + result.counts["keep"] > 0
    for verdict in result.of(Verdict.TRASH):
        assert verdict.valuation.unpriceable or verdict.total_chaos < 10.0


def test_the_strictness_setting_is_read_and_a_bad_value_falls_back_loudly(appraised_stack):
    module = appraised_stack.get("appraisal")
    assert module.strictness() is Strictness.GENEROUS
    appraised_stack.settings.set("appraisal", "strictness", "strict")
    assert module.strictness() is Strictness.STRICT


def test_extra_bases_extend_the_allowlist_rather_than_replacing_it(appraised_stack):
    module = appraised_stack.get("appraisal")
    before = module.allowlist()
    appraised_stack.settings.set("appraisal", "extra_high_value_bases", ["Sadist Garb"])
    after = module.allowlist()
    assert before < after
    assert "Sadist Garb" in after


# -- appraising the fixture bag ------------------------------------------------


async def test_every_verdict_path_is_reachable_on_one_bag(appraiser, loot):
    result = await appraiser.appraise(loot)
    assert all(count > 0 for count in result.counts.values()), result.counts


async def test_the_two_strictness_levels_disagree_on_the_same_bag(appraiser, loot):
    """The two *gates*, with tier 3 held out of the comparison.

    ``escalate=False`` on both sides deliberately: since Phase 4b the generous run
    also *prices* what it flags, so a like-for-like comparison of the two gates has
    to switch that off or it is comparing two different questions.
    """
    generous = await appraiser.appraise(loot, strictness=Strictness.GENEROUS, escalate=False)
    strict = await appraiser.appraise(loot, strictness=Strictness.STRICT, escalate=False)

    assert generous.counts["check"] > strict.counts["check"]
    assert generous.counts["trash"] < strict.counts["trash"]
    # Everything the strict gate flags, the generous gate flags too.
    strict_flagged = {i.uid for i in strict.escalation_candidates}
    generous_flagged = {i.uid for i in generous.escalation_candidates}
    assert strict_flagged < generous_flagged
    # And no threshold moved, so the money is identical either way.
    assert generous.total_chaos == strict.total_chaos
    assert generous.counts["unpriceable"] == strict.counts["unpriceable"]
    assert (generous.trade_requests, strict.trade_requests) == (0, 0)


async def test_the_total_excludes_unpriceable_and_worn_equipment(appraised_stack, appraiser):
    """Two ways a total goes wrong at once. The fixture's worn Headhunter is worth
    8,977c, so a bag total that swallowed it would be off by a visible amount."""
    bag = await appraised_stack.api(PoeApi).get_items()
    result = await appraiser.appraise(bag.by_source(Source.BAG))

    priced = sum(i.total_chaos for i in result.items if not i.unpriceable)
    assert result.total_chaos == pytest.approx(priced)
    assert result.unpriceable_stack > 0
    assert not any(i.name == "Headhunter" for i in result.items)
    assert any(i.location.slot == "Belt" for i in bag.items)


async def test_the_bag_total_is_the_sum_of_the_blocks(appraiser, loot):
    result = await appraiser.appraise(loot)
    blocks = sum(
        item.total_chaos
        for verdict in (Verdict.KEEP, Verdict.CHECK, Verdict.TRASH)
        for item in result.of(verdict)
    )
    assert result.total_chaos == pytest.approx(blocks)


async def test_appraise_item_agrees_with_appraise_on_the_same_item(appraiser, loot):
    result = await appraiser.appraise(loot)
    for row in result.items[:6]:
        single = await appraiser.appraise_item(
            next(i for i in loot if i.uid == row.uid)
        )
        assert single.verdict is row.verdict, row.name


def test_the_gate_is_reachable_without_pricing_anything(appraiser, loot):
    """Synchronous, no valuation, no request — the arithmetic a bag screen can run
    while the price tables are still loading."""
    flagged = [i for i in loot if appraiser.gate(i).passed]
    assert flagged
    assert all(appraiser.gate(i, strictness=Strictness.STRICT).passed for i in loot
               if appraiser.gate(i, strictness=Strictness.STRICT).passed)


# -- the request budget --------------------------------------------------------


async def test_a_stash_run_is_never_escalated_however_it_is_asked(
    appraised_stack, server, loot
):
    """SPEC §5.3's rule, which survives Phase 4b in the place it was actually about.

    A bag of five rares fits inside ``5:10:60``. A stash of 818 items does not, and
    that is a property of the stash rather than of the caller's intent — so
    ``escalate=True`` at strict strictness is refused, not obeyed."""
    appraiser = appraised_stack.api(AppraisalApi)
    before = len(server.requests)
    result = await appraiser.appraise(loot, strictness=Strictness.STRICT, escalate=True)

    sent = [r.url.path for r in server.requests[before:]]
    assert not any(p.startswith("/api/trade/search/") for p in sent), sent
    assert not any(p.startswith("/character-window/") for p in sent), sent
    assert result.trade_requests == 0
    # The gate still flagged what it would query, and still queried none of it.
    assert result.escalation_candidates


async def test_a_bag_run_prices_the_gates_rares_and_stops_at_the_budget(
    appraised_stack, server, loot
):
    """The other half of the same rule: a *bag* is escalated, and bounded."""
    appraised_stack.settings.set("appraisal", "max_eager_quotes", 2)
    appraiser = appraised_stack.api(AppraisalApi)
    result = await appraiser.appraise(loot, strictness=Strictness.GENEROUS)

    searches = [r for r in server.trade_requests() if "/api/trade/search/" in r.url.path]
    assert len(searches) == 2, "the budget is a cap, not a suggestion"
    assert len(result.escalation_candidates) > 2, "and there was more it could have asked"
    priced = [
        item
        for item in result.items
        if item.valuation.price is not None
        and item.valuation.price.source is PriceSource.TRADE
    ]
    assert priced, "nothing came back with a trade price"
    assert all(item.verdict is not Verdict.UNPRICEABLE for item in priced)


async def test_escalation_is_off_when_the_budget_is_zero(appraised_stack, server, loot):
    appraised_stack.settings.set("appraisal", "max_eager_quotes", 0)
    result = await appraised_stack.api(AppraisalApi).appraise(loot)
    assert result.trade_requests == 0
    assert not any("/api/trade/search/" in r.url.path for r in server.trade_requests())


async def test_appraising_the_bag_end_to_end_spends_one_item_request(
    appraised_stack, server
):
    """The `appraisal.appraise_bag` method fetches the bag and prices it.

    The bag fetch is the only **account** call. Trade requests share the hostname
    and share nothing else: no credential, a different policy, a different bucket.
    """
    before = len(server.to_host("www.pathofexile.com"))
    await appraised_stack.methods.call("appraisal.appraise_bag")
    after = server.to_host("www.pathofexile.com")[before:]

    account = [r for r in after if r.url.path.startswith("/character-window/")]
    assert {r.url.path for r in account} <= {
        "/character-window/get-items",
        "/character-window/get-characters",
    }
    assert all(
        "cookie" not in {k.lower() for k in r.headers} for r in server.trade_requests()
    )


# -- the method surface --------------------------------------------------------


async def test_appraise_bag_json_is_serializable_and_carries_the_four_counts(
    appraised_stack,
):
    import json

    payload = await appraised_stack.methods.call("appraisal.appraise_bag")
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload["counts"]) == {"keep", "check", "trash", "unpriceable"}
    assert payload["unpriceable_stack"] > 0
    assert payload["trade_requests"] <= payload["escalation_candidates"] * 2
    assert payload["character"]
    assert payload["pricing_count"] >= 0
    assert payload["total_is_floor"] is (payload["pricing_count"] > 0)


async def test_appraise_bag_json_accepts_a_strictness_and_rejects_a_bad_one(
    appraised_stack,
):
    strict = await appraised_stack.methods.call("appraisal.appraise_bag", None, "strict")
    assert strict["strictness"] == "strict"
    with pytest.raises(AppraisalError, match="unknown strictness"):
        await appraised_stack.methods.call("appraisal.appraise_bag", None, "lenient")


async def test_the_gate_method_takes_a_uid_not_an_item(appraised_stack, loot):
    """So the frontend cannot hand the backend an item it invented — the same rule
    `prices.quote_json` follows."""
    uid = next(i.uid for i in loot if i.base_type == "Hubris Circlet")
    result = await appraised_stack.methods.call("appraisal.gate", uid)
    assert result["passed"] is True
    assert {s["name"] for s in result["signals"]} >= {"ilvl_86", "high_value_base"}
    with pytest.raises(AppraisalError, match="no item"):
        await appraised_stack.methods.call("appraisal.gate", "not-a-real-uid")


async def test_the_settings_method_reports_what_the_verdicts_were_made_with(
    appraised_stack,
):
    payload = await appraised_stack.methods.call("appraisal.settings")
    assert payload["keep_threshold_chaos"] == DEFAULT_KEEP_CHAOS
    assert payload["strictness"] == "generous"
    assert "Hubris Circlet" in payload["high_value_bases"]


async def test_it_announces_a_completed_appraisal(appraised_stack):
    seen = []
    appraised_stack.events.subscribe(APPRAISAL_COMPLETE, seen.append)
    await appraised_stack.methods.call("appraisal.appraise_bag")
    assert len(seen) == 1
    payload = seen[0].payload
    assert set(payload["counts"]) == {"keep", "check", "trash", "unpriceable"}
    assert payload["threshold_chaos"] == DEFAULT_KEEP_CHAOS
    assert payload["unpriceable_stack"] > 0


# -- degradation ---------------------------------------------------------------


async def test_with_no_price_tables_everything_is_a_gate_decision(
    stack_factory, registry, server, cache_clock
):
    """poe.ninja being down must not produce a bag of `trash`. Without tables the
    indexable rows are honestly unpriceable, the rares still get a gate verdict, and
    tier 0 keeps working — a ``~b/o 40 chaos`` note needs no table, because chaos is
    the unit rather than a line in one."""
    from modules.prices.backend.module import PricesModule

    server.bag_fixture = "loot-bag.json"
    server.ninja_status = 503
    await stack_factory(PricesModule(clock=cache_clock, prefetch=False), AppraisalModule())
    try:
        bag = await registry.api(PoeApi).get_items()
        # `escalate=False`: with no tables at all, tier 3 would be the *only* thing
        # pricing anything, and this test is about what survives when it is not.
        result = await registry.api(AppraisalApi).appraise(
            bag.by_source(Source.BAG), escalate=False
        )
        assert result.counts["unpriceable"] > 5, "every indexable row is now a hole"
        assert result.counts["check"] > 0, "the gate still works without prices"
        kept = result.of(Verdict.KEEP)
        assert [i.name for i in kept] == ["Brood Locket"], "only the player's own note survives"
        assert result.total_chaos == pytest.approx(40.0)
        assert result.divine_rate is None, "no currency table, so no divine figure"
        assert result.total_divine is None
    finally:
        await registry.stop_all()


async def test_an_empty_bag_appraises_to_four_zeroes(appraiser):
    result = await appraiser.appraise([])
    assert result.counts == {"keep": 0, "check": 0, "trash": 0, "unpriceable": 0}
    assert result.total_chaos == 0.0
    assert result.ranked() == []


# -- tier 3 that does not answer -------------------------------------------------


async def test_a_rare_nobody_is_selling_stays_unpriceable_and_is_never_zero(
    appraised_stack, server, loot
):
    """The whole point of the four-state model, met by the newest tier.

    A search that finds nothing is a real answer and a different one from "worth
    nothing". The row keeps no price, contributes nothing to the total, and says it
    is still pricing rather than pretending the question is closed.
    """
    server.trade_search_empty = True
    result = await appraised_stack.api(AppraisalApi).appraise(loot)

    pending = result.pricing
    assert pending, "nothing was escalated at all"
    for item in pending:
        assert item.valuation.unpriceable
        assert item.total_chaos == 0.0
        assert item.verdict is Verdict.CHECK
        assert "pricing…" in item.reason
    assert result.total_is_floor
    # ...and the money is still only what we actually know.
    assert result.total_chaos == pytest.approx(
        sum(i.total_chaos for i in result.items if not i.valuation.unpriceable)
    )


async def test_a_slow_quote_costs_a_number_never_the_output(
    appraised_stack, loot, monkeypatch
):
    """SPEC §5.3: a per-item ``pricing…`` state that never gates the grid.

    The quote is made to hang. The pass must still return, with every other verdict
    intact and the hung rows marked — not blocked, and not silently dropped."""
    import asyncio

    prices = appraised_stack.get("prices")

    async def never(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(prices, "quote", never)
    appraised_stack.settings.set("appraisal", "eager_timeout_seconds", 0.1)

    result = await asyncio.wait_for(
        appraised_stack.api(AppraisalApi).appraise(loot), timeout=10
    )
    assert result.pricing, "a hung quote left no trace"
    assert result.counts["keep"] > 0, "the rest of the bag came through"
    assert result.total_is_floor
