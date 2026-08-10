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
from modules.moddb.backend.api import ModDbApi
from modules.poeapi.backend.api import PoeApi, Source
from modules.prices.backend.api import PricesApi
from runtime.errors import ModuleNotStartedError

# -- declaration and wiring ----------------------------------------------------


def test_the_declaration_matches_the_plan_except_where_it_says_why():
    module = AppraisalModule()
    assert (module.id, module.kind) == ("appraisal", "feature")
    assert module.provides is AppraisalApi
    # `prices` is the plan's dependency; `poeapi` is Phase 4's documented addition
    # and `moddb` is Phase 9's. This assertion is here so removing one is deliberate.
    assert set(module.requires) == {"prices", "poeapi", "moddb"}
    assert isinstance(module, AppraisalApi)


def test_it_resolves_every_dependency_at_start(appraised_stack):
    module = appraised_stack.get("appraisal")
    assert isinstance(module._prices, PricesApi)
    assert isinstance(module._poeapi, PoeApi)
    assert isinstance(module._moddb, ModDbApi)


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
    assert names == [
        "appraisal.appraise_bag",
        # Phase 10's two, and note what is *not* here: no crawl. A crawl is minutes of
        # the account's item budget, and it lives behind a press in the CLI rather
        # than behind a dispatch any surface can make (SPEC §6.6).
        "appraisal.appraise_tab",
        "appraisal.gate",
        "appraisal.highlight",
        "appraisal.price_check",
        "appraisal.settings",
        "appraisal.stash_digest",
    ]


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


def test_extra_bases_extend_the_opinion_list_rather_than_replacing_it(appraised_stack):
    module = appraised_stack.get("appraisal")
    before = module.sought_bases()
    appraised_stack.settings.set("appraisal", "extra_sought_after_bases", ["Sadist Garb"])
    after = module.sought_bases()
    assert before < after
    assert "Sadist Garb" in after


# -- appraising the fixture bag ------------------------------------------------


async def test_every_verdict_path_is_reachable_on_one_bag(appraiser, loot):
    result = await appraiser.appraise(loot)
    assert all(count > 0 for count in result.counts.values()), result.counts


async def test_the_two_strictness_levels_disagree_on_the_same_bag(appraiser, loot):
    """The two highlighters, on one bag.

    No escalation switch to hold out any more: neither run makes a trade request, so
    the comparison is between two readings of the same items and nothing else.
    """
    generous = await appraiser.appraise(loot, strictness=Strictness.GENEROUS)
    strict = await appraiser.appraise(loot, strictness=Strictness.STRICT)

    assert generous.counts["check"] > strict.counts["check"]
    assert generous.counts["trash"] < strict.counts["trash"]
    # Everything the strict gate flags, the generous gate flags too.
    strict_flagged = {i.uid for i in strict.highlighted}
    generous_flagged = {i.uid for i in generous.highlighted}
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


async def test_an_appraise_issues_zero_trade_requests(appraised_stack, server, loot):
    """The claim Phase 9 makes, counted at the wire rather than read off the code.

    Not a setting, not a strictness, not a parameter: **there is no escalation path
    left**. `appraisal` has no ``escalate`` argument, no eager budget and no timeout,
    and the only thing that can reach the trade API from here is
    :meth:`AppraisalApi.price_check`, which a player has to ask for.
    """
    appraiser = appraised_stack.api(AppraisalApi)
    for level in Strictness:
        before = len(server.requests)
        result = await appraiser.appraise(loot, strictness=level)
        sent = [r.url.path for r in server.requests[before:]]
        assert not any(p.startswith("/api/trade/search/") for p in sent), sent
        assert not any(p.startswith("/api/trade/fetch/") for p in sent), sent
        assert result.trade_requests == 0
        # ...and it still says which rows it would have been about.
        if level is Strictness.GENEROUS:
            assert result.highlighted


def test_the_appraise_signature_has_no_escalate_parameter():
    """A deleted path that leaves its switch behind grows the path back.

    `appraise` is the method a zone transition calls on every map, so an
    ``escalate=True`` still sitting in the signature is one settings change away from
    the behaviour §5b removed.
    """
    import inspect

    assert "escalate" not in inspect.signature(AppraisalModule.appraise).parameters
    assert "escalate" not in inspect.signature(AppraisalModule.appraise_bag_json).parameters


async def test_a_highlighted_rare_is_left_without_a_price_and_the_total_says_so(
    appraiser, loot
):
    """The honest replacement for the eager pass.

    The old code made this hole small by spending requests on it, badly. Now the hole
    is named: the rows are counted, the total is a floor, and nothing invents a
    number for them.
    """
    result = await appraiser.appraise(loot)
    unchecked = result.unchecked
    assert unchecked, "no rare was highlighted at all"
    for item in unchecked:
        assert item.valuation.unpriceable
        assert item.total_chaos == 0.0
        assert item.valuation.price is None
        assert item.verdict is Verdict.CHECK
        assert "worth asking about" in item.reason
    assert result.total_is_floor
    assert not result.pricing, "nothing is outstanding; the floor is about the unasked"


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


async def test_appraise_bag_json_is_serializable_and_carries_every_count(
    appraised_stack,
):
    import json

    payload = await appraised_stack.methods.call("appraisal.appraise_bag")
    assert json.loads(json.dumps(payload)) == payload
    assert set(payload["counts"]) == {"keep", "check", "trash", "unpriceable", "not_loot"}
    assert payload["unpriceable_stack"] > 0
    assert payload["trade_requests"] == 0, "an appraise never asks the trade API"
    assert payload["character"]
    assert payload["pricing_count"] == 0
    assert payload["highlighted_count"] >= payload["unchecked_count"] > 0
    assert payload["total_is_floor"] is True


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
    # A Hubris Circlet tops out at affix level 85, so an ilvl-86 one is fully rolled
    # and carries GGG's own top-tier tag. It is also unidentified in the fixture.
    assert {s["name"] for s in result["signals"]} >= {"top_tier_base", "unidentified"}
    with pytest.raises(AppraisalError, match="no item"):
        await appraised_stack.methods.call("appraisal.gate", "not-a-real-uid")


async def test_the_settings_method_reports_what_the_verdicts_were_made_with(
    appraised_stack,
):
    payload = await appraised_stack.methods.call("appraisal.settings")
    assert payload["keep_threshold_chaos"] == DEFAULT_KEEP_CHAOS
    assert payload["strictness"] == "generous"
    assert "Stygian Vise" in payload["sought_after_bases"]
    # ...and, since every tier on the screen comes out of it, how old the database is.
    assert "mod database for Path of Exile" in payload["moddb"]


async def test_it_announces_a_completed_appraisal(appraised_stack):
    seen = []
    appraised_stack.events.subscribe(APPRAISAL_COMPLETE, seen.append)
    await appraised_stack.methods.call("appraisal.appraise_bag")
    assert len(seen) == 1
    payload = seen[0].payload
    assert set(payload["counts"]) == {"keep", "check", "trash", "unpriceable", "not_loot"}
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
    from modules.moddb.backend.module import ModDbModule
    from modules.prices.backend.module import PricesModule

    server.bag_fixture = "loot-bag.json"
    server.ninja_status = 503
    await stack_factory(
        PricesModule(clock=cache_clock, prefetch=False), ModDbModule(), AppraisalModule()
    )
    try:
        bag = await registry.api(PoeApi).get_items()
        result = await registry.api(AppraisalApi).appraise(bag.by_source(Source.BAG))
        assert result.counts["unpriceable"] > 5, "every indexable row is now a hole"
        assert result.counts["check"] > 0, "the highlighter still works without prices"
        kept = result.of(Verdict.KEEP)
        assert [i.name for i in kept] == ["Brood Locket"], "only the player's own note survives"
        assert result.total_chaos == pytest.approx(40.0)
        assert result.divine_rate is None, "no currency table, so no divine figure"
        assert result.total_divine is None
    finally:
        await registry.stop_all()


async def test_an_empty_bag_appraises_to_all_zeroes(appraiser):
    result = await appraiser.appraise([])
    assert result.counts == {"keep": 0, "check": 0, "trash": 0, "unpriceable": 0, "not_loot": 0}
    assert result.total_chaos == 0.0
    assert result.ranked() == []


