"""The stash at `poeapi` level: tab kinds, layouts, the cache rules, the crawl.

Everything here is offline, against ``tests/fixtures/stash/`` — an eight-tab stash
modelled on the measured account (research-notes §7) rather than invented. The three
claims worth proving with request counts rather than with reading are:

* a **remove-only** tab is fetched once, ever;
* a **map** tab is reported *not supported*, never as an empty tab worth 0c;
* a **digest** over every tab fetches no items at all.
"""

from __future__ import annotations

import pytest

from modules.poeapi.backend.api import (
    MAP_UNSUPPORTED,
    QUAD_COLUMNS,
    STANDARD_COLUMNS,
    PoeApi,
    TabKind,
)
from modules.poeapi.backend.stash import FOREVER, layout_for, tab_kind, tab_ttl
from tests.conftest import FakeClock, Server, stash_requests

# -- kinds and layouts ---------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("NormalStash", TabKind.NORMAL),
        ("PremiumStash", TabKind.PREMIUM),
        ("QuadStash", TabKind.QUAD),
        ("CurrencyStash", TabKind.CURRENCY),
        ("DivinationCardStash", TabKind.DIVINATION),
        ("MapStash", TabKind.MAP),
        ("Folder", TabKind.FOLDER),
        ("SomethingGGGAddedStash", TabKind.UNKNOWN),
        ("", TabKind.UNKNOWN),
    ],
)
def test_tab_types_map_to_kinds(raw: str, expected: TabKind):
    assert tab_kind(raw) is expected


def test_a_quad_tab_is_24_by_24_and_a_normal_one_is_12_by_12():
    """The measured quad held 112 items and is four normal tabs, not a bigger one."""
    assert layout_for(TabKind.QUAD).cols == QUAD_COLUMNS
    assert layout_for(TabKind.QUAD).rows == QUAD_COLUMNS
    assert layout_for(TabKind.NORMAL).cols == STANDARD_COLUMNS
    assert layout_for(TabKind.PREMIUM).rows == STANDARD_COLUMNS


def test_special_tabs_declare_no_lattice_at_all():
    """Currency, essence, fragment and card tabs place items in bespoke slots.

    One item per grid cell is simply not true of them — ``stackSize`` legitimately
    exceeds ``maxStackSize`` there — so the layout says so rather than inventing a
    lattice a surface would then draw wrongly.
    """
    for kind in (TabKind.CURRENCY, TabKind.ESSENCE, TabKind.FRAGMENT, TabKind.DIVINATION):
        assert layout_for(kind).grid is False
        assert layout_for(kind).cols is None


def test_an_unknown_tab_type_degrades_to_a_list_rather_than_a_wrong_grid():
    assert layout_for(TabKind.UNKNOWN).grid is False


# -- the tab list --------------------------------------------------------------


async def test_the_tab_list_carries_kinds_layouts_and_the_remove_only_flag(stash_api: PoeApi):
    tabs = await stash_api.get_stash_tabs("Standard")
    by_index = {tab.index: tab for tab in tabs.all_tabs()}
    assert by_index[0].kind is TabKind.CURRENCY
    assert by_index[2].kind is TabKind.QUAD
    assert by_index[2].layout.cols == QUAD_COLUMNS
    assert [tab.index for tab in tabs.all_tabs() if tab.remove_only] == [3, 6]


async def test_the_tab_list_is_a_tree_with_a_flat_fast_path(stash_api: PoeApi):
    """No folders were observed on the measured account, so the tree is one level.

    The model is a tree anyway — folders exist in the game — but nothing is *built*
    on speculation: with no parents, ``tabs`` and ``all_tabs()`` are the same list.
    """
    tabs = await stash_api.get_stash_tabs("Standard")
    assert [tab.index for tab in tabs.tabs] == [tab.index for tab in tabs.all_tabs()]
    assert all(not tab.children for tab in tabs.tabs)


# -- remove-only: the highest-leverage rule in the feature ----------------------


def test_tab_ttl_is_infinite_for_a_remove_only_tab():
    from modules.poeapi.backend.models import StashTab

    assert tab_ttl(StashTab(index=1, remove_only=True), 20.0) == FOREVER
    assert tab_ttl(StashTab(index=1), 20.0) == 20.0
    assert tab_ttl(None, 20.0) == 20.0


async def test_a_remove_only_tab_is_fetched_once_ever(
    stash_api: PoeApi, server: Server, cache_clock: FakeClock
):
    """research-notes §7: 86% of the measured Standard stash cannot gain items.

    A day later it is still the same tab, and asking again would spend an item
    request to learn nothing. This is the rule that takes a full refresh from ~34
    minutes to ~45 seconds.
    """
    await stash_api.get_stash_tabs("Standard")
    first = await stash_api.get_stash_items(3, "Standard")
    assert [item.name for item in first.items] == ["Veiled Scarab"]

    cache_clock.advance(86_400)
    again = await stash_api.get_stash_items(3, "Standard")
    assert again.meta.from_cache is True
    assert stash_requests(server).count(3) == 1
    assert [item.stack_size for item in again.items] == [170]


async def test_refresh_still_reaches_a_remove_only_tab(stash_api: PoeApi, server: Server):
    """It cannot *gain* items — it can still lose them, and that goes through the
    player. So the rule is a TTL, not a refusal."""
    await stash_api.get_stash_tabs("Standard")
    await stash_api.get_stash_items(3, "Standard")
    await stash_api.get_stash_items(3, "Standard", refresh=True)
    assert stash_requests(server).count(3) == 2


async def test_an_ordinary_tab_expires_normally(
    stash_api: PoeApi, server: Server, cache_clock: FakeClock
):
    await stash_api.get_stash_tabs("Standard")
    await stash_api.get_stash_items(1, "Standard")
    cache_clock.advance(3600)
    await stash_api.get_stash_items(1, "Standard")
    assert stash_requests(server).count(1) == 2


# -- map tabs: the unresolved one ----------------------------------------------


async def test_a_map_tab_reports_unsupported_rather_than_empty(stash_api: PoeApi):
    """The failure mode this phase was warned about, in one assertion.

    Five map tabs across two leagues have answered with no items. GGG's own stash API
    models a map tab as a parent with children fetched one at a time, so the likeliest
    reading of that zero is *not traversed*. An empty tab and an unreadable one look
    identical in a total and mean opposite things, so this one says which it is.
    """
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(4, "Standard")
    assert tab.items == []
    assert tab.unsupported == MAP_UNSUPPORTED
    assert "not supported yet" in tab.unsupported


async def test_an_unsupported_tab_does_not_even_spend_a_request(
    stash_api: PoeApi, server: Server
):
    await stash_api.get_stash_tabs("Standard")
    await stash_api.get_stash_items(4, "Standard")
    assert 4 not in stash_requests(server)


async def test_a_map_tab_that_did_return_items_would_be_shown(
    stash_api: PoeApi, server: Server
):
    """It is the *zero* that is untrustworthy, not the tab.

    If GGG changes the endpoint — or if this reading of it is simply wrong — the tool
    must show what came back rather than keep insisting it cannot exist.
    """
    from modules.poeapi.backend.stash import unsupported_reason

    assert unsupported_reason(TabKind.MAP, item_count=0) == MAP_UNSUPPORTED
    assert unsupported_reason(TabKind.MAP, item_count=7) is None
    assert unsupported_reason(TabKind.PREMIUM, item_count=0) is None


async def test_the_first_tab_a_player_opens_costs_exactly_one_request(
    stash_api: PoeApi, server: Server
):
    """SPEC §6.6's primary path, on a completely cold cache.

    Every stash response carries the tab list alongside the items, so a cold open
    files both from one request. Fetching the list separately would make "one
    request, about a second" true only from the second tab onwards.
    """
    tab = await stash_api.get_stash_items(1, "Standard")
    assert len(tab.items) == 6
    assert len(stash_requests(server)) == 1
    # ...and the list it carried is now on disk, so the digest is free too.
    state = await stash_api.stash_state("Standard")
    assert len(state.tabs) == 8
    assert len(stash_requests(server)) == 1


# -- placement ------------------------------------------------------------------


async def test_a_quad_tab_places_an_item_in_the_far_corner(stash_api: PoeApi):
    """On a 12x12 lattice this item is off the board and silently disappears."""
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(2, "Standard")
    corner = max(tab.items, key=lambda item: item.grid.x)
    assert (corner.grid.x, corner.grid.y) == (QUAD_COLUMNS - 1, QUAD_COLUMNS - 1)
    assert corner.stack_size == 77


async def test_a_special_tab_keeps_a_stack_size_above_its_own_maximum(stash_api: PoeApi):
    """``Vaal Orb 163/20`` is a measured row, not a corruption of the fixture.

    Special tabs stack past the item's ordinary ceiling, so anything that "corrected"
    this — clamping, or assuming one item per cell — would understate a currency tab
    by an order of magnitude.
    """
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(0, "Standard")
    vaal = next(item for item in tab.items if item.name == "Vaal Orb")
    assert vaal.stack_size == 163
    assert vaal.max_stack_size == 20
    assert vaal.stack_size > (vaal.max_stack_size or 0)


# -- per-tab staleness ----------------------------------------------------------


async def test_stash_state_reports_freshness_per_tab_without_fetching_any(
    stash_api: PoeApi, server: Server, cache_clock: FakeClock
):
    await stash_api.get_stash_tabs("Standard")
    await stash_api.get_stash_items(1, "Standard")
    cache_clock.advance(60)
    before = len(stash_requests(server))

    state = await stash_api.stash_state("Standard")
    by_index = {entry.tab.index: entry for entry in state.tabs}
    assert by_index[1].cached is True
    assert by_index[1].age_seconds == pytest.approx(60, abs=1)
    assert by_index[1].stale is True  # a live tab's copy is 20 seconds good
    assert by_index[1].item_count == 6
    # Never read: not cached, and `item_count` is None rather than 0.
    assert by_index[7].cached is False
    assert by_index[7].item_count is None
    assert by_index[7].needs_fetch is True
    assert len(stash_requests(server)) == before


async def test_a_remove_only_tab_is_permanent_and_never_stale(
    stash_api: PoeApi, cache_clock: FakeClock
):
    await stash_api.get_stash_tabs("Standard")
    await stash_api.get_stash_items(3, "Standard")
    cache_clock.advance(86_400 * 30)
    state = await stash_api.stash_state("Standard")
    entry = next(entry for entry in state.tabs if entry.tab.index == 3)
    assert entry.permanent is True
    assert entry.stale is False
    assert entry.needs_fetch is False


# -- the cost of a full refresh -------------------------------------------------


async def test_the_cost_is_computed_against_the_real_tab_list_and_falls_as_it_fills(
    stash_api: PoeApi,
):
    """SPEC §6.6: a crawl states its cost honestly up front — and keeps it honest.

    A warning that still says "~30 minutes" after the crawl has finished is a warning
    people learn to ignore, so the figure is derived from what is still missing.
    """
    await stash_api.get_stash_tabs("Standard")
    cold = (await stash_api.stash_state("Standard")).cost
    # Seven readable tabs; the map tab is not one of them.
    assert cold.requests == 7
    assert cold.unsupported_tabs == 1
    assert "min" in cold.warning or "s," in cold.warning
    assert "pause your inventory syncing" in cold.warning

    await stash_api.get_stash_items(3, "Standard")
    warm = (await stash_api.stash_state("Standard")).cost
    assert warm.requests == 6
    assert warm.permanent_tabs == 2


async def test_the_estimate_is_computed_against_the_buckets_not_the_sustained_rate(
    stash_api: PoeApi,
):
    """A small refresh is seconds. Quoting the sustained rate would call it minutes.

    GGG's item policy is ``30:60`` **and** ``100:1800``: thirty requests fit in the
    first minute and the hundred-and-first costs half an hour. Multiplying by the
    sustained figure — one per 18 s — overstates every small refresh by an order of
    magnitude, and a warning that does that is one people press through.
    """
    await stash_api.get_stash_tabs("Standard")
    state = await stash_api.stash_state("Standard")
    assert state.seconds_per_request > 0  # still reported: it is the *forever* rate
    # Four: the Account rule and the Ip rule each publish both windows, and both
    # bind. Keeping them all is why the estimate cannot be fooled by whichever the
    # limiter happens to list first.
    assert sorted({b.period for b in state.buckets}) == [60.0, 1800.0]
    assert len(state.buckets) == 4
    # Seven tabs fit inside the first minute's allowance, so the honest answer is
    # seconds — not 7 x 18.
    assert state.cost.requests == 7
    assert state.cost.seconds == pytest.approx(7.0)
    assert "s," in state.cost.warning


def test_the_estimate_turns_a_full_stash_into_the_half_hour_it_really_is():
    """117 tabs is the measured Standard account, and it is the long bucket that
    bites: the first hundred requests are minutes, the hundred-and-first is 1800 s."""
    from modules.poeapi.backend.stash import estimate_seconds

    buckets = [(29, 60.0), (97, 1800.0)]  # the measured policy, with the safety margin
    assert estimate_seconds(15, buckets, 18.0) == pytest.approx(15.0)
    assert estimate_seconds(29, buckets, 18.0) == pytest.approx(29.0)
    assert estimate_seconds(30, buckets, 18.0) == pytest.approx(60.0)
    assert estimate_seconds(107, buckets, 18.0) == pytest.approx(1800.0)
    # ...and with nothing learned it falls back to the pessimistic sustained rate,
    # which is what the limiter's seed budget will actually do.
    assert estimate_seconds(15, [], 18.0) == pytest.approx(14 * 18.0)
    assert estimate_seconds(0, buckets, 18.0) == 0.0


# -- the crawl ------------------------------------------------------------------


async def test_a_crawl_walks_every_readable_tab_and_skips_the_unreadable_one(
    stash_api: PoeApi,
):
    steps = [step async for step in stash_api.crawl_stash("Standard")]
    assert [step.tab.index for step in steps] == [0, 1, 2, 3, 5, 6, 7]
    assert all(step.error is None for step in steps)
    assert sum(len(step.items.items) for step in steps if step.items) == 16


async def test_a_crawl_is_resumable_and_writes_its_bookmark_after_every_tab(
    stash_api: PoeApi, server: Server
):
    """A crawl killed at minute 28 must not start again at tab 1.

    Interrupted here by walking only the first three tabs, which is what an aborted
    process leaves behind: a bookmark on disk naming what is already done.
    """
    walked = []
    async for step in stash_api.crawl_stash("Standard", limit=3):
        walked.append(step.tab.index)
    assert walked == [0, 1, 2]

    progress = await stash_api.crawl_progress("Standard")
    assert progress is not None
    assert progress.done == [0, 1, 2]
    assert progress.requests == 3

    resumed = [step.tab.index async for step in stash_api.crawl_stash("Standard")]
    assert resumed == [3, 5, 6, 7]
    assert (await stash_api.crawl_progress("Standard")).done == [0, 1, 2, 3, 5, 6, 7]
    # Nothing was fetched twice.
    assert sorted(index for index in stash_requests(server)) == [0, 0, 1, 2, 3, 5, 6, 7]


async def test_a_second_crawl_costs_nothing_for_the_remove_only_tabs(
    stash_api: PoeApi, server: Server
):
    """The 34-minutes-to-45-seconds rule, as a number a caller can print."""
    async for _ in stash_api.crawl_stash("Standard"):
        pass
    spent_before = len(stash_requests(server))

    steps = [step async for step in stash_api.crawl_stash("Standard", resume=False)]
    from_cache = [step.tab.index for step in steps if step.from_cache]
    assert 3 in from_cache and 6 in from_cache
    assert sum(step.spent for step in steps) == len(stash_requests(server)) - spent_before


async def test_a_failing_tab_is_recorded_and_the_crawl_continues(
    stash_api: PoeApi, server: Server
):
    """One bad tab must not cost the other 116.

    The failure is recorded on the bookmark rather than raised, so a resumed crawl
    knows to come back to it — and the step carries the reason, so a caller can print
    which tab went wrong instead of a crawl that quietly returned fewer tabs.
    """
    await stash_api.get_stash_tabs("Standard")
    steps = []
    async for step in stash_api.crawl_stash("Standard", limit=3):
        steps.append(step)
        server.status = 500  # every tab after the first one fails
    server.status = 200

    assert steps[0].error is None
    assert all("500" in (step.error or "") for step in steps[1:])
    progress = await stash_api.crawl_progress("Standard")
    assert progress.done == [steps[0].tab.index]
    assert progress.failed == [step.tab.index for step in steps[1:]]

    # ...and a resumed crawl comes back to them.
    resumed = [step.tab.index async for step in stash_api.crawl_stash("Standard")]
    assert set(step.tab.index for step in steps[1:]) <= set(resumed)


async def test_nothing_starts_a_crawl_by_itself(stash_stack, server: Server):
    """SPEC §6.6, as a property of the registered surface rather than a promise.

    Starting the whole stack — which is what a `poedex serve` does — must not fetch a
    single stash tab, and there is no registered method that could start a crawl.
    """
    assert stash_requests(server) == []
    assert "poeapi.crawl_stash" not in stash_stack.methods.for_module("poeapi")
