"""The stash, judged: strictness, composition, and the holes a total must not fill.

Phase 10 adds no new judgement. It reuses the highlighter and the manual price check
over items that live in a stash tab instead of in a backpack, at the other strictness,
and these tests are mostly about proving that *reuse* rather than a parallel path:
the same ``highlight`` builds the same checkbox list with the same ``MAX_PRETICKED``,
and the same ``price_check`` runs the player's own selection.

What is genuinely new is the arithmetic over tabs, and every assertion about it is
about a hole: an unread tab and an unreadable one are unknown, not zero.
"""

from __future__ import annotations

import pytest

from modules.appraisal.backend.api import (
    AppraisalApi,
    Composition,
    Selection,
    Strictness,
    Verdict,
)
from modules.appraisal.backend.highlight import MAX_PRETICKED
from modules.appraisal.backend.stash import classify
from modules.poeapi.backend.api import MAP_UNSUPPORTED, PoeApi
from tests.conftest import Server, stash_requests

# -- strictness: the same gate, the opposite bias ------------------------------


async def test_the_stash_is_strict_and_the_bag_is_generous_by_default(stash_stack):
    module = stash_stack.get("appraisal")
    assert module.strictness() is Strictness.GENEROUS
    assert module.stash_strictness() is Strictness.STRICT


async def test_loosening_the_bag_gate_does_not_loosen_the_stash(stash_stack):
    """Two settings because they answer different questions.

    A player who wants a chattier bag panel has said nothing about whether they want
    800 stash items flagged, and SPEC §5.2 is explicit that a generous gate at that
    scale is all noise.
    """
    module = stash_stack.get("appraisal")
    stash_stack.settings.set("appraisal", "strictness", "strict")
    assert module.strictness() is Strictness.STRICT
    stash_stack.settings.set("appraisal", "stash_strictness", "generous")
    assert module.stash_strictness() is Strictness.GENEROUS


async def test_strict_and_generous_diverge_on_the_same_stash_item(
    stash_appraiser: AppraisalApi, stash_api: PoeApi
):
    """``Soul Bind`` is the divergence, and it is a real fixture item.

    A Siege Helmet with a near-top-tier roll: T2 on a ladder long enough that the
    generous gate calls it interesting, and no *hard* signal at all. In a bag that is
    a row worth a second look; in a stash of 818 items it is the false positive that
    makes the whole digest unreadable.
    """
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(1, "Standard")
    helmet = next(item for item in tab.items if item.name == "Soul Bind")

    generous = stash_appraiser.gate(helmet, strictness=Strictness.GENEROUS)
    strict = stash_appraiser.gate(helmet, strictness=Strictness.STRICT)
    assert generous.passed is True
    assert strict.passed is False
    assert [signal.name for signal in generous.signals] == ["tier:0"]
    assert all(not signal.hard for signal in generous.signals)


async def test_a_hard_signal_survives_strictness(
    stash_appraiser: AppraisalApi, stash_api: PoeApi
):
    """Strict drops the soft signals, not the facts. A six-link is a six-link."""
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(1, "Standard")
    armour = next(item for item in tab.items if item.name == "Corpse Guardian")
    strict = stash_appraiser.gate(armour, strictness=Strictness.STRICT)
    assert [signal.name for signal in strict.signals] == ["six_link"]


async def test_the_tab_appraisal_uses_the_strict_gate(stash_appraiser: AppraisalApi):
    result = await stash_appraiser.appraise_tab(1, league="Standard")
    assert result.appraisal.strictness is Strictness.STRICT
    names = {item.name for item in result.appraisal.highlighted}
    assert "Soul Bind" not in names
    assert "Corpse Guardian" in names


async def test_the_strictness_can_still_be_overridden_for_one_call(
    stash_appraiser: AppraisalApi,
):
    result = await stash_appraiser.appraise_tab(
        1, league="Standard", strictness=Strictness.GENEROUS
    )
    assert {item.name for item in result.appraisal.highlighted} >= {"Soul Bind"}


# -- composition ---------------------------------------------------------------


def test_classify_is_the_gates_own_predicate_and_has_no_threshold():
    assert classify([]) is Composition.EMPTY


async def test_a_currency_tab_is_bulk_and_no_item_in_it_enters_the_gate(
    stash_appraiser: AppraisalApi,
):
    """SPEC §5.2: bulk tabs never enter tier 2 or 3. On a real stash that is most of
    it — the measured account's largest tabs are 214 cards and 133 fragments."""
    result = await stash_appraiser.appraise_tab(0, league="Standard")
    assert result.summary.composition is Composition.BULK
    assert all(not item.gate.considered for item in result.appraisal.items)
    assert result.appraisal.highlighted == []


async def test_a_gear_tab_is_gear(stash_appraiser: AppraisalApi):
    """Rares are spatially segregated on a real account, because players sort."""
    result = await stash_appraiser.appraise_tab(1, league="Standard")
    assert result.summary.composition is Composition.GEAR
    assert all(item.gate.considered for item in result.appraisal.items)


async def test_a_mixed_tab_is_neither(stash_appraiser: AppraisalApi):
    result = await stash_appraiser.appraise_tab(2, league="Standard")
    assert result.summary.composition is Composition.BULK  # oils, decks, alchs


# -- the digest: what it costs, and what it refuses to claim -------------------


async def test_the_digest_spends_no_item_requests(
    stash_appraiser: AppraisalApi, stash_api: PoeApi, server: Server
):
    """Opening a stash screen must not become a crawl, whatever the tab count.

    This is the difference between one cached tab-list read and 117 requests, and it
    is why the digest reads ``cached_stash_items`` rather than ``get_stash_items``.
    """
    await stash_api.get_stash_tabs("Standard")
    before = len(stash_requests(server))
    digest = await stash_appraiser.stash_digest("Standard")
    assert len(stash_requests(server)) == before
    assert len(digest.tabs) == 8


async def test_an_unread_tab_is_a_hole_and_not_a_zero(
    stash_appraiser: AppraisalApi, stash_api: PoeApi
):
    await stash_api.get_stash_tabs("Standard")
    digest = await stash_appraiser.stash_digest("Standard")
    unread = {tab.tab.index for tab in digest.unread}
    assert 7 in unread
    row = next(tab for tab in digest.tabs if tab.tab.index == 7)
    assert row.known is False
    assert row.hole is True
    # `composition` is null on the wire rather than "empty": a tab nobody has opened
    # is not a tab with nothing in it, and one of those two is worth going to look at.
    assert row.to_json()["composition"] is None
    assert row.to_json()["item_count"] is None
    assert digest.total_is_floor is True


async def test_a_map_tab_is_a_hole_for_the_other_reason(
    stash_appraiser: AppraisalApi, stash_api: PoeApi
):
    await stash_api.get_stash_tabs("Standard")
    digest = await stash_appraiser.stash_digest("Standard")
    row = next(tab for tab in digest.tabs if tab.tab.index == 4)
    assert row.supported is False
    assert row.hole is True
    assert row.to_json()["unsupported_reason"] == MAP_UNSUPPORTED
    assert [tab.tab.index for tab in digest.unsupported] == [4]


async def test_the_digest_totals_what_has_been_read(
    stash_appraiser: AppraisalApi, stash_api: PoeApi
):
    await stash_api.get_stash_tabs("Standard")
    empty = await stash_appraiser.stash_digest("Standard")
    assert empty.total_chaos == 0.0

    await stash_api.get_stash_items(0, "Standard")
    filled = await stash_appraiser.stash_digest("Standard")
    currency = next(tab for tab in filled.tabs if tab.tab.index == 0)
    assert currency.known is True
    assert currency.total_chaos > 0
    assert filled.total_chaos == pytest.approx(currency.total_chaos)
    # ...and it is still a floor, because six tabs are unread and one is unreadable.
    assert filled.total_is_floor is True


async def test_a_removed_item_in_a_remove_only_tab_stays_unpriceable(
    stash_appraiser: AppraisalApi,
):
    """research-notes §7: Standard holds ~170 of a removed item poe.ninja's league
    index does not carry. Calling that trash — or summing it as zero — understates
    the stash badly, and it is the same rule the bag already follows."""
    result = await stash_appraiser.appraise_tab(3, league="Standard")
    scarab = result.appraisal.items[0]
    assert scarab.name == "Veiled Scarab"
    assert scarab.verdict is Verdict.UNPRICEABLE
    assert result.appraisal.unpriceable_stack == 170
    assert result.appraisal.total_chaos == 0.0


# -- an unsupported tab, appraised ---------------------------------------------


async def test_appraising_a_map_tab_says_unsupported_rather_than_showing_nothing(
    stash_appraiser: AppraisalApi,
):
    result = await stash_appraiser.appraise_tab(4, league="Standard")
    assert result.supported is False
    assert result.unsupported == MAP_UNSUPPORTED
    assert result.appraisal.items == []
    assert result.to_json()["unsupported"] == MAP_UNSUPPORTED


# -- the manual price check, unchanged ------------------------------------------


async def test_highlighting_a_stash_item_is_the_same_call_as_a_bag_item(
    stash_stack, stash_api: PoeApi
):
    """Same method, same checkbox list, same cap. Phase 10 forks nothing."""
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(1, "Standard")
    armour = next(item for item in tab.items if item.name == "Corpse Guardian")

    module = stash_stack.get("appraisal")
    direct = module.highlight(armour)
    through_method = await stash_stack.methods.call(
        "appraisal.highlight", uid=armour.uid, tab_index=1
    )
    assert through_method == direct.to_json()
    assert len(direct.preticked) <= MAX_PRETICKED


async def test_a_stash_price_check_runs_the_players_selection(
    stash_stack, stash_api: PoeApi, server: Server
):
    await stash_api.get_stash_tabs("Standard")
    tab = await stash_api.get_stash_items(1, "Standard")
    armour = next(item for item in tab.items if item.name == "Corpse Guardian")
    module = stash_stack.get("appraisal")
    proposal = module.highlight(armour)

    before = len(server.trade_requests())
    result = await module.price_check(armour, Selection(uid=armour.uid, mods=(0,)))
    assert result.selection.mods == (0,)
    assert result.highlight.uid == proposal.uid
    # A check spends, and says how much. Nothing else in this file does.
    assert len(server.trade_requests()) > before
    assert result.spent > 0


async def test_a_stash_item_cannot_be_reached_without_naming_its_tab(stash_stack):
    """Scanning every tab for a uid would be up to 117 requests behind a call that
    looks free — the auto-crawl in disguise. A surface that showed the item knows
    which tab it came from."""
    from modules.appraisal.backend.api import AppraisalError

    with pytest.raises(AppraisalError):
        await stash_stack.methods.call("appraisal.highlight", uid="nonexistent", tab_index=1)


# -- the wire ------------------------------------------------------------------


async def test_the_tab_payload_carries_its_layout(stash_appraiser: AppraisalApi):
    """A quad is 24x24 and a currency tab has no lattice — both have to reach the
    screen, or it draws three quarters of a quad and an invented currency grid."""
    quad = (await stash_appraiser.appraise_tab(2, league="Standard")).to_json()["tab"]
    assert (quad["cols"], quad["rows"], quad["grid"]) == (24, 24, True)
    currency = (await stash_appraiser.appraise_tab(0, league="Standard")).to_json()["tab"]
    assert (currency["cols"], currency["grid"]) == (None, False)


async def test_the_digest_payload_states_the_cost_of_the_rest(
    stash_appraiser: AppraisalApi,
):
    payload = (await stash_appraiser.stash_digest("Standard")).to_json()
    assert payload["cost"]["requests"] == 7
    assert "pause your inventory syncing" in payload["cost"]["warning"]
    assert payload["unread_count"] + payload["known_count"] + payload["unsupported_count"] == 8
