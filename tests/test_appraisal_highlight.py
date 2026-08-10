"""Phase 9 — the checkbox list and the manual price check.

The pivot in two halves. :class:`ItemHighlight` is the *proposal*: which mods an item
has, what tier each roll is **on this base**, which lines the database refuses to
name a tier for, and how many affix slots are free. :class:`Selection` is the
player's answer, and it is the only thing the trade query is built from.

The property this file exists to protect is narrow and easy to lose: **the query
comes from the selection, not from the item and not from the gate.** Both of the
alternatives were tried live and both failed — every mod ANDed matched zero listings,
one loose mod matched a single worse item and reported its asking price as a median.
"""

from __future__ import annotations

import pytest

from modules.appraisal.backend.api import (
    TIER_UNKNOWN,
    AppraisalApi,
    AppraisalError,
    Selection,
)
from modules.appraisal.backend.gate import evaluate, report_for
from modules.appraisal.backend.highlight import build as build_highlight
from modules.moddb.backend.module import ModDbModule
from modules.poeapi.backend.api import PoeApi
from tests.test_appraisal_gate import item


@pytest.fixture(scope="module")
def db():
    return ModDbModule()


def highlight_for(subject, db):
    report = report_for(subject, db)
    return build_highlight(subject, evaluate(subject, moddb=db, report=report), report, moddb=db)


HELMET = dict(base_type="Siege Helmet", category="armour", subcategory="helmet", ilvl=86)


# -- the list itself -------------------------------------------------------------


def test_every_readable_line_becomes_a_tickable_row(db):
    subject = item(
        **HELMET,
        explicit=["+120 to maximum Life", "+30% to Fire Resistance"],
        crafted=["+15 to maximum Mana"],
    )
    options = highlight_for(subject, db).mods
    assert [option.text for option in options] == [
        "+120 to maximum Life",
        "+30% to Fire Resistance",
        "+15 to maximum Mana",
    ]
    # Indexes are the identity, not the text: two identical suffixes are a real item.
    assert [option.index for option in options] == [0, 1, 2]


def test_a_tier_is_shown_only_where_moddb_asserted_one(db):
    """The honesty rule, on one item with both kinds of line.

    ``+120 to maximum Life`` on a Siege Helmet is T2 of 10 and says so. ``10%
    increased Rarity of Items found`` could have come from more than one ladder, so
    the label is the word ``unknown`` and there is no number anywhere on the row.
    """
    subject = item(
        **HELMET,
        explicit=["+120 to maximum Life", "10% increased Rarity of Items found"],
    )
    life, rarity = highlight_for(subject, db).mods
    assert life.tier_label == "T2 of 10"
    assert (life.tier, life.tiers) == (2, 10)

    assert rarity.tier_label == TIER_UNKNOWN
    assert rarity.tier is None
    assert rarity.tiers is None
    assert rarity.attribution == "ambiguous"
    assert rarity.preticked is False, "an unattributed line must not be pre-ticked"


def test_the_same_roll_labels_differently_on_two_bases(db):
    """What the deleted thresholds could not do, seen from the panel's side."""
    helmet = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        explicit=["+95 to maximum Life"],
    )
    body = item(
        base_type="Astral Plate",
        category="armour",
        subcategory="body_armour",
        ilvl=86,
        explicit=["+95 to maximum Life"],
    )
    assert highlight_for(helmet, db).mods[0].tier_label == "T4 of 10"
    assert highlight_for(body, db).mods[0].tier_label == "T7 of 13"
    assert highlight_for(helmet, db).mods[0].ceiling == 144.0
    assert highlight_for(body, db).mods[0].ceiling == 189.0


def test_top_tier_rolls_are_preticked_and_mediocre_ones_are_not(db):
    subject = item(**HELMET, explicit=["+130 to maximum Life", "+12% to Fire Resistance"])
    great, poor = highlight_for(subject, db).mods
    assert great.top_tier and great.preticked
    assert not poor.preticked


def test_an_influence_mod_is_preticked_whatever_its_tier(db):
    """It is the rarest thing on the item and the reason anyone would buy it."""
    subject = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        influences=["warlord"],
        explicit=["Nearby Enemies have -9% to Fire Resistance"],
    )
    option = highlight_for(subject, db).mods[0]
    assert option.influences == ("warlord",)
    assert option.preticked


def test_the_highlight_carries_no_price_and_no_estimate(db):
    """A proposal that arrived with a number attached would be the automatic pricing
    this phase deleted, wearing a different name."""
    subject = item(**HELMET, explicit=["+130 to maximum Life"])
    payload = highlight_for(subject, db).to_json()
    assert "chaos" not in payload
    assert "price" not in payload
    assert not any("chaos" in key for key in payload)


def test_the_open_affix_counts_come_with_their_own_confidence(db):
    subject = item(**HELMET, explicit=["+120 to maximum Life", "+30% to Fire Resistance"])
    proposal = highlight_for(subject, db)
    assert (proposal.open_prefixes, proposal.open_suffixes) == (2, 2)
    assert proposal.counts_are_certain is True

    # ...and a line whose prefix/suffix side cannot be decided makes the counts a
    # floor rather than a fact, which whoever builds the filter has to know.
    murky = item(**HELMET, explicit=["10% increased Rarity of Items found"])
    assert highlight_for(murky, db).counts_are_certain is False


def test_a_line_the_offline_bridge_cannot_name_is_annotated_not_disabled(db):
    """The bridge has real holes, so it may annotate and must not veto.

    ``98% increased Energy Shield`` is a common mod with no entry in the trimmed
    artifact. Greying its checkbox out would silently narrow what the player is
    allowed to ask — the same failure as building the query for them, in the other
    direction — so the live stat document decides and ``build_plan`` reports whatever
    it could not resolve.
    """
    subject = item(
        base_type="Vaal Regalia",
        category="armour",
        subcategory="body_armour",
        ilvl=84,
        explicit=["+112 to maximum Energy Shield", "98% increased Energy Shield"],
    )
    flat, percent = highlight_for(subject, db).mods
    assert flat.tradeable is True
    assert percent.tradeable is False, "the artifact's bridge really is missing this"

    # ...and it still reaches the query, where the live index gets the last word.
    proposal = highlight_for(subject, db)
    spec = Selection(uid=subject.uid, mods=(0, 1)).spec(proposal)
    assert [focus.text for focus in spec.mods] == [
        "+112 to maximum Energy Shield",
        "98% increased Energy Shield",
    ]


def test_without_a_database_the_lines_survive_untiered(db):
    """A missing artifact makes a worse offer, not an empty one."""
    subject = item(**HELMET, explicit=["+120 to maximum Life"])
    proposal = build_highlight(subject, evaluate(subject, moddb=None), None, moddb=None)
    assert [option.text for option in proposal.mods] == ["+120 to maximum Life"]
    assert proposal.mods[0].tier_label == TIER_UNKNOWN
    assert proposal.mods[0].value == 120.0
    assert not proposal.preticked


# -- the selection becomes the query ---------------------------------------------


def test_the_query_is_built_from_the_ticks_and_not_from_the_item(db):
    """Six mods on the item, two ticked, two filters — and they are the ticked two."""
    subject = item(
        **HELMET,
        explicit=[
            "+120 to maximum Life",
            "+30% to Fire Resistance",
            "+25% to Cold Resistance",
            "+28% to Lightning Resistance",
            "+40 to Strength",
            "10% increased Rarity of Items found",
        ],
    )
    proposal = highlight_for(subject, db)
    spec = Selection(uid=subject.uid, mods=(1, 4)).spec(proposal)
    assert [focus.text for focus in spec.mods] == ["+30% to Fire Resistance", "+40 to Strength"]


def test_a_ticked_roll_searches_a_widened_floor_not_an_exact_match(db):
    """``103 → 82``. Exact matching is what returned zero listings twice."""
    subject = item(**HELMET, explicit=["+120 to maximum Life"])
    proposal = highlight_for(subject, db)
    spec = Selection(uid=subject.uid, mods=(0,)).spec(proposal)
    assert spec.mods[0].minimum == 96
    assert spec.mods[0].minimum < 120


def test_a_ticked_line_with_no_number_becomes_a_presence_filter(db):
    subject = item(**HELMET, explicit=["Cannot be Frozen"])
    proposal = highlight_for(subject, db)
    spec = Selection(uid=subject.uid, mods=(0,)).spec(proposal)
    assert spec.mods and spec.mods[0].minimum is None


def test_the_open_affix_option_reaches_the_spec(db):
    subject = item(**HELMET, explicit=["+120 to maximum Life"])
    proposal = highlight_for(subject, db)
    spec = Selection(uid=subject.uid, mods=(), open_prefixes=1, open_suffixes=2).spec(proposal)
    assert (spec.open_prefixes, spec.open_suffixes) == (1, 2)
    assert spec.asks_anything
    # None is "do not ask", which is different from asking for zero.
    assert Selection(uid=subject.uid).spec(proposal).open_prefixes is None


def test_an_index_the_item_does_not_have_is_ignored(db):
    """The selection is indexes into *this* item's list, and nothing else."""
    subject = item(**HELMET, explicit=["+120 to maximum Life"])
    proposal = highlight_for(subject, db)
    assert Selection(uid=subject.uid, mods=(7,)).spec(proposal).mods == ()


def test_a_selection_never_broadens_itself(db):
    subject = item(**HELMET, explicit=["+120 to maximum Life"])
    proposal = highlight_for(subject, db)
    assert Selection(uid=subject.uid, mods=(0,)).spec(proposal).broaden is False


def test_the_default_selection_is_the_pre_ticked_set(db):
    subject = item(**HELMET, explicit=["+130 to maximum Life", "+12% to Fire Resistance"])
    proposal = highlight_for(subject, db)
    assert proposal.selection().mods == proposal.preticked == (0,)


# -- through the module, against the offline stack --------------------------------


async def test_a_price_check_spends_two_requests_and_an_appraise_spends_none(
    appraised_stack, server, loot
):
    appraiser = appraised_stack.api(AppraisalApi)
    subject = next(i for i in loot if i.base_type == "Vaal Regalia")

    def searches():
        # The tier-1b bulk exchange and the static rate table share the trade
        # *hostname* and nothing else — different route, different bucket, no
        # credential. What "eager tier 3" ever meant is a /search/.
        return [
            r
            for r in server.trade_requests()
            if r.url.path.startswith(("/api/trade/search/", "/api/trade/fetch/"))
        ]

    before = len(searches())
    await appraiser.appraise(loot)
    assert len(searches()) == before, "an appraise asked the trade API"

    result = await appraiser.price_check(subject)
    spent = searches()[before:]
    # One search, one fetch. No broadening retry.
    assert sum(1 for r in spent if "/search/" in r.url.path) == 1
    assert sum(1 for r in spent if "/fetch/" in r.url.path) == 1
    assert result.spent >= 2
    assert result.selection.mods == result.highlight.preticked


async def test_a_check_with_nothing_ticked_is_refused_rather_than_widened(
    appraised_stack, server, loot
):
    """"Price this with no filters" is a base-type search, and the price of the
    cheapest junk sharing a base is the confidently wrong number §5b is about."""
    appraiser = appraised_stack.api(AppraisalApi)
    subject = next(i for i in loot if i.base_type == "Coral Ring")
    before = len(server.trade_requests())
    with pytest.raises(AppraisalError, match="nothing was selected"):
        await appraiser.price_check(subject, Selection(uid=subject.uid))
    assert len(server.trade_requests()) == before, "a refused check still spent a request"


async def test_the_price_check_method_takes_a_uid_and_indexes_not_mod_text(
    appraised_stack, loot
):
    """So the frontend cannot submit an item it invented, or a filter the panel never
    drew. The same rule ``prices.quote_json`` follows."""
    uid = next(i.uid for i in loot if i.base_type == "Vaal Regalia")
    payload = await appraised_stack.methods.call("appraisal.price_check", uid, [0])
    assert payload["uid"] == uid
    assert payload["selection"]["mods"] == [0]
    assert "comparables" in payload
    with pytest.raises(AppraisalError, match="no item"):
        await appraised_stack.methods.call("appraisal.price_check", "not-a-real-uid")


async def test_a_check_that_matches_nothing_says_so_and_invents_no_price(
    appraised_stack, server, loot
):
    server.trade_search_empty = True
    appraiser = appraised_stack.api(AppraisalApi)
    subject = next(i for i in loot if i.base_type == "Vaal Regalia")
    result = await appraiser.price_check(subject)
    assert result.chaos is None
    assert result.priced is False
    assert "untick a mod" in result.reason
    assert result.to_json()["chaos"] is None


async def test_a_thin_sample_is_labelled_rather_than_reported_as_a_market_price(
    appraised_stack, loot
):
    """The 10c-for-a-1c-jewel failure, made visible instead of prevented.

    A median over a handful of listings is a real answer and sometimes the only one
    available. What must never happen again is it arriving *unlabelled*.
    """
    appraiser = appraised_stack.api(AppraisalApi)
    subject = next(i for i in loot if i.base_type == "Vaal Regalia")
    result = await appraiser.price_check(subject)
    if result.priced and result.comparables < 5:
        assert result.thin
        assert "too few" in result.reason
    assert str(result.comparables) in result.reason


async def test_the_highlight_method_costs_nothing(appraised_stack, server, loot):
    uid = next(i.uid for i in loot if i.base_type == "Hubris Circlet")
    before = len(server.requests)
    payload = await appraised_stack.methods.call("appraisal.highlight", uid)
    after = [r.url.path for r in server.requests[before:]]
    assert not any("/api/trade/" in path for path in after)
    assert payload["highlighted"] is True
    assert payload["uid"] == uid


async def test_the_bag_and_the_highlight_agree_about_which_rows_are_flagged(
    appraised_stack, loot
):
    appraiser = appraised_stack.api(AppraisalApi)
    result = await appraiser.appraise(loot)
    flagged = {row.uid for row in result.highlighted}
    by_uid = {i.uid: i for i in loot}
    for uid in flagged:
        assert appraiser.highlight(by_uid[uid]).highlighted


async def test_a_stale_bag_still_yields_a_uid_the_check_can_use(appraised_stack, loot):
    """The uid is `poeapi`'s stable fingerprint, so a highlight taken now and a check
    run a moment later are about the same item."""
    bag = await appraised_stack.api(PoeApi).get_items()
    assert {i.uid for i in loot} <= {i.uid for i in bag.items}
