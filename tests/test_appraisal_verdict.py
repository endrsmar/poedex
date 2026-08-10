"""The four verdicts, the thresholds, and the one state that must never collapse.

Half of this file exists for a single claim: `unpriceable` is not a low-value variant
of `trash`, does not become `trash` at any threshold, does not enter the total as
zero, and survives every ordering, serialization and tally the module performs on it.
It is tested that many ways because it fails *silently* — a bag with a 170-unit hole
in it looks exactly like a correct bag, and research-notes §7 measured that hole on
the real account.
"""

from __future__ import annotations

import pytest

from modules.appraisal.backend.api import (
    DEFAULT_CHECK_CHAOS,
    DEFAULT_KEEP_CHAOS,
    BagAppraisal,
    GateResult,
    GateSignal,
    Strictness,
    Verdict,
    indexable,
)
from modules.appraisal.backend.verdict import appraise_bag, appraise_one, classify
from modules.poeapi.backend.api import Rarity
from modules.prices.backend.api import (
    BagValuation,
    Price,
    PriceSource,
    Tier3,
    Valuation,
)
from tests.test_appraisal_gate import item

PASSED = GateResult([GateSignal("influence", "shaper-influenced", hard=True)])
MISSED = GateResult([])
NOT_CONSIDERED = GateResult([], considered=False)


def valuation(
    chaos: float | None,
    *,
    stack: int = 1,
    source: PriceSource = PriceSource.BULK,
    reason: str | None = None,
    name: str = "Thing",
) -> Valuation:
    price = None if chaos is None else Price(chaos, source)
    return Valuation(
        uid="uid-thing",
        name=name,
        base_type=name,
        category="currency",
        stack_size=stack,
        price=price,
        reason=reason,
    )


def currency(name: str):
    """A stack of something the bulk index is *supposed* to carry.

    ``item()`` defaults to a rare ring, and a rare ring with no price is a
    non-question rather than a hole — so the unpriceable tests have to say what they
    mean rather than only changing the rarity.
    """
    return item(name=name, base_type=name, category="currency", subcategory=None,
                rarity=Rarity.CURRENCY)


def verdict_of(subject, val, gate=MISSED, *, keep=DEFAULT_KEEP_CHAOS, check=DEFAULT_CHECK_CHAOS):
    return classify(subject, val, gate, keep_chaos=keep, check_chaos=check)[0]


# -- the threshold, exactly at the boundary ------------------------------------


@pytest.mark.parametrize(
    ("chaos", "expected"),
    [
        (19.99, Verdict.CHECK),
        (20.0, Verdict.KEEP),  # "at or above" — SPEC §5.4
        (20.01, Verdict.KEEP),
        (1.0, Verdict.CHECK),  # the check floor, also inclusive
        (0.99, Verdict.TRASH),
        (0.0, Verdict.TRASH),
    ],
)
def test_the_threshold_boundaries_are_inclusive_from_below(chaos, expected):
    assert verdict_of(item(), valuation(chaos)) is expected


def test_the_threshold_is_a_parameter_and_moving_it_moves_the_verdict():
    """The number 20 appears once in this module, as a named default. Everything
    else takes it as an argument, and this is what proves it."""
    subject, val = item(), valuation(50.0)
    assert verdict_of(subject, val, keep=DEFAULT_KEEP_CHAOS) is Verdict.KEEP
    assert verdict_of(subject, val, keep=100.0) is Verdict.CHECK
    assert verdict_of(subject, val, keep=1000.0) is Verdict.CHECK
    assert verdict_of(subject, val, keep=1000.0, check=100.0) is Verdict.TRASH


def test_the_threshold_is_compared_against_the_stack_not_the_unit_price():
    """`Jeweller's Orb x2615` is worth a stash trip and one Jeweller's Orb is not."""
    cheap_stack = valuation(0.01025, stack=2615)
    assert cheap_stack.total_chaos == pytest.approx(26.8, rel=1e-3)
    assert verdict_of(item(), cheap_stack) is Verdict.KEEP
    assert verdict_of(item(), valuation(0.01025, stack=1)) is Verdict.TRASH


# -- unpriceable ---------------------------------------------------------------


def test_an_indexable_item_with_no_price_is_unpriceable_not_trash():
    scarab = item(
        name="Veiled Scarab",
        base_type="Veiled Scarab",
        category="currency",
        subcategory=None,
        rarity=Rarity.CURRENCY,
    )
    result, reason = classify(
        scarab,
        valuation(None, stack=170, name="Veiled Scarab", reason="not in the index"),
        NOT_CONSIDERED,
        keep_chaos=DEFAULT_KEEP_CHAOS,
        check_chaos=DEFAULT_CHECK_CHAOS,
    )
    assert result is Verdict.UNPRICEABLE
    assert "index" in reason


@pytest.mark.parametrize("keep", [0.0, 0.5, 20.0, 1_000_000.0])
def test_no_threshold_can_turn_unpriceable_into_anything_else(keep):
    """Including a threshold of zero, at which *every* priced item is `keep`."""
    scarab = item(
        name="Veiled Scarab",
        base_type="Veiled Scarab",
        category="currency",
        subcategory=None,
        rarity=Rarity.CURRENCY,
    )
    assert verdict_of(scarab, valuation(None, stack=170), keep=keep, check=keep) is (
        Verdict.UNPRICEABLE
    )


@pytest.mark.parametrize(
    ("category", "rarity", "expected"),
    [
        ("currency", Rarity.CURRENCY, True),
        ("fragment", Rarity.NORMAL, True),
        ("card", Rarity.DIVINATION, True),
        ("map", Rarity.NORMAL, True),
        ("map", Rarity.RARE, True),  # a rare map is still priced by tier
        ("armour", Rarity.UNIQUE, True),
        ("accessory", Rarity.RELIC, True),
        # ...and the other side of the line: bulk never priced these.
        ("accessory", Rarity.RARE, False),
        ("weapon", Rarity.MAGIC, False),
        ("jewel", Rarity.RARE, False),
        ("something_new_ggg_invented", Rarity.NORMAL, False),
    ],
)
def test_indexable_draws_the_line_between_a_hole_and_a_non_question(
    category, rarity, expected
):
    subject = item(category=category, subcategory=None, rarity=rarity)
    assert indexable(subject) is expected


def test_a_rare_with_no_bulk_price_is_never_unpriceable():
    """No bulk table has ever priced a rare. Calling that a gap in poe.ninja's
    coverage would fill the panel with question marks and hide the real gaps."""
    rare = item(base_type="Coral Ring", rarity=Rarity.RARE)
    assert verdict_of(rare, valuation(None), PASSED) is Verdict.CHECK
    assert verdict_of(rare, valuation(None), MISSED) is Verdict.TRASH


def test_unpriceable_rows_are_excluded_from_the_total_and_counted_separately():
    bag = _bag(
        [
            (currency("Divine Orb"), valuation(897.7), MISSED),
            (
                currency("Veiled Scarab"),
                valuation(None, stack=170, name="Veiled Scarab"),
                NOT_CONSIDERED,
            ),
        ]
    )
    assert bag.total_chaos == pytest.approx(897.7)
    assert bag.counts["unpriceable"] == 1
    assert bag.unpriceable_stack == 170
    assert bag.counts["trash"] == 0


def test_the_serialized_bag_never_reports_an_unpriceable_row_as_worth_zero():
    """A frontend reads `to_json`, not the dataclass. The `unpriceable` flag and the
    separate count are the only things stopping a chart from plotting a 0."""
    bag = _bag(
        [
            (
                currency("Veiled Scarab"),
                valuation(None, stack=170, name="Veiled Scarab"),
                NOT_CONSIDERED,
            )
        ]
    )
    payload = bag.to_json()
    assert payload["total_chaos"] == 0
    assert payload["unpriceable_count"] == 1
    assert payload["unpriceable_stack"] == 170
    assert payload["items"][0]["unpriceable"] is True
    assert payload["items"][0]["verdict"] == "unpriceable"
    assert "total_including_unpriceable" not in payload


def test_every_verdict_is_always_present_in_the_tally():
    """A tally built by counting what is there loses the states that are not, and a
    panel that drops the `unpriceable` line when a bag happens to have none teaches
    the player it does not exist. The same now goes for `not_loot`."""
    empty = BagAppraisal(
        [], league="Standard", threshold_chaos=20.0, strictness=Strictness.GENEROUS
    )
    assert empty.counts == {"keep": 0, "check": 0, "trash": 0, "unpriceable": 0, "not_loot": 0}


# -- the gate's effect on a verdict --------------------------------------------


def test_a_gate_hit_promotes_a_cheap_item_to_check_but_never_to_keep():
    """`keep` is a claim about value, and the gate knows no values — that is why it
    is consulted at all."""
    assert verdict_of(item(), valuation(0.5), PASSED) is Verdict.CHECK
    assert verdict_of(item(), valuation(0.5), MISSED) is Verdict.TRASH
    assert verdict_of(item(), valuation(19.0), PASSED) is Verdict.CHECK


def test_a_priced_item_over_the_threshold_stays_keep_whatever_the_gate_says():
    assert verdict_of(item(), valuation(500.0), PASSED) is Verdict.KEEP
    assert verdict_of(item(), valuation(500.0), MISSED) is Verdict.KEEP


def test_strictness_changes_the_verdict_of_the_same_item():
    """The end-to-end version of the gate's divergence: a real verdict flips."""
    ring = item(
        name="Loath Grip",
        base_type="Coral Ring",
        ilvl=81,
        explicit=[
            "+79 to maximum Life",
            "+41% to Fire Resistance",
            "+39% to Cold Resistance",
            "+38% to Lightning Resistance",
        ],
    )
    from modules.appraisal.backend.gate import evaluate

    generous = appraise_one(
        ring,
        valuation(None),
        evaluate(ring, strictness=Strictness.GENEROUS),
        keep_chaos=20.0,
        check_chaos=1.0,
    )
    strict = appraise_one(
        ring,
        valuation(None),
        evaluate(ring, strictness=Strictness.STRICT),
        keep_chaos=20.0,
        check_chaos=1.0,
    )
    assert generous.verdict is Verdict.CHECK
    assert strict.verdict is Verdict.TRASH
    assert generous.escalate and not strict.escalate


def test_the_reason_is_never_just_the_verdict_repeated():
    for chaos, gate in [(500.0, MISSED), (5.0, MISSED), (0.1, MISSED), (None, PASSED)]:
        _, reason = classify(
            item(base_type="Coral Ring", rarity=Rarity.RARE),
            valuation(chaos),
            gate,
            keep_chaos=20.0,
            check_chaos=1.0,
        )
        assert reason and reason not in {v.value for v in Verdict}


# -- ordering and assembly -----------------------------------------------------


def test_ranking_puts_the_interesting_rows_first():
    bag = _bag(
        [
            (currency("junk"), valuation(0.01, name="junk"), MISSED),
            (currency("hole"), valuation(None, name="hole"), MISSED),
            (currency("rich"), valuation(900.0, name="rich"), MISSED),
            (currency("minor"), valuation(3.0, name="minor"), MISSED),
        ]
    )
    assert [i.name for i in bag.ranked()] == ["rich", "minor", "hole", "junk"]


def test_within_a_block_hard_signals_outrank_soft_ones():
    """A `check` block is mostly rows worth 0c; without this tie-break a six-linked
    influenced rare sorts level with a rare that has three mediocre mods."""
    soft = GateResult([GateSignal("mod_group", "mods present: max life")])
    bag = _bag(
        [
            (item(name="soft", base_type="Coral Ring"), valuation(None, name="soft"), soft),
            (item(name="hard", base_type="Coral Ring"), valuation(None, name="hard"), PASSED),
        ]
    )
    assert [i.name for i in bag.ranked()] == ["hard", "soft"]


def test_ranking_is_total_so_two_runs_never_disagree():
    rows = [
        (currency(n), valuation(5.0, name=n), MISSED)
        for n in ("c", "a", "b")
    ]
    assert [i.name for i in _bag(rows).ranked()] == ["a", "b", "c"]


def test_mismatched_sequence_lengths_are_an_error_not_a_silent_mispairing():
    subject = item()
    valued = BagValuation([valuation(1.0)], league="Standard")
    with pytest.raises(ValueError, match="one valuation and one gate"):
        appraise_bag(
            [subject, subject], valued, [MISSED, MISSED], keep_chaos=20.0, check_chaos=1.0,
            strictness=Strictness.GENEROUS,
        )


def test_the_appraisal_carries_the_threshold_that_produced_it():
    """Without it, a stored or transmitted appraisal is uninterpretable: the same
    bag is 3 keeps or 30 depending on a number that is not in the payload."""
    bag = _bag([(item(), valuation(5.0), MISSED)], keep=7.5)
    assert bag.threshold_chaos == 7.5
    assert bag.to_json()["threshold_chaos"] == 7.5
    assert bag.to_json()["strictness"] == "generous"


def _bag(rows, *, keep: float = DEFAULT_KEEP_CHAOS, check: float = DEFAULT_CHECK_CHAOS):
    items = [row[0] for row in rows]
    valued = BagValuation(
        [row[1] for row in rows], league="Standard", divine_rate=897.7, lookups=len(rows)
    )
    return appraise_bag(
        items,
        valued,
        [row[2] for row in rows],
        keep_chaos=keep,
        check_chaos=check,
        strictness=Strictness.GENEROUS,
    )


# -- bug 2: "pricing…" that never ended ----------------------------------------
#
# `classify` branched on one boolean that meant both "tier 3 is outstanding" and
# "tier 3 answered, and the answer was nothing". A zero-result search therefore
# rendered `pricing…` forever, promising a number that was never coming.


def _rare_ring(**kwargs):
    return item(**kwargs)


def _tier3(state: Tier3, *, reason: str | None = None) -> Valuation:
    return Valuation(
        uid="uid-rare",
        name="Corpse Loop",
        base_type="Amethyst Ring",
        category="accessory",
        stack_size=1,
        price=None,
        tier3=state,
        reason=reason,
    )


def test_bug2_an_outstanding_query_and_an_empty_answer_are_different_states():
    """The model first. A boolean cannot hold this distinction, which is why it did
    not hold it."""
    pending = _tier3(Tier3.PENDING)
    empty = _tier3(Tier3.NO_LISTINGS)

    assert pending.pricing and not pending.no_listings
    assert empty.no_listings and not empty.pricing
    # Both still have no price, and neither is worth zero.
    assert pending.unpriceable and empty.unpriceable
    assert pending.total_chaos == 0.0 and empty.total_chaos == 0.0


def test_bug2_a_finished_empty_search_never_renders_as_pricing():
    subject = _rare_ring()
    _verdict, reason = classify(
        subject,
        _tier3(Tier3.NO_LISTINGS),
        PASSED,
        keep_chaos=DEFAULT_KEEP_CHAOS,
        check_chaos=DEFAULT_CHECK_CHAOS,
    )
    assert "pricing…" not in reason
    assert "no matching listings" in reason
    # The gate's reasoning survives beside it: the row is still worth a look.
    assert "shaper-influenced" in reason


def test_bug2_an_outstanding_query_still_says_pricing():
    """The other half. Fixing the lie must not delete the true case."""
    subject = _rare_ring()
    _verdict, reason = classify(
        subject,
        _tier3(Tier3.PENDING),
        PASSED,
        keep_chaos=DEFAULT_KEEP_CHAOS,
        check_chaos=DEFAULT_CHECK_CHAOS,
    )
    assert "pricing…" in reason


def test_bug2_both_states_stay_check_because_the_player_still_looks():
    for state in (Tier3.PENDING, Tier3.NO_LISTINGS, Tier3.FAILED):
        assert verdict_of(_rare_ring(), _tier3(state), PASSED) is Verdict.CHECK


def test_bug2_a_query_that_could_not_run_is_its_own_terminal_state():
    """Rate limited or offline is finished-for-this-pass too, and for a reason about
    us rather than about the market."""
    val = _tier3(Tier3.FAILED, reason="the trade API is rate limited; retry in 42s")
    _verdict, reason = classify(
        _rare_ring(), val, PASSED, keep_chaos=DEFAULT_KEEP_CHAOS, check_chaos=DEFAULT_CHECK_CHAOS
    )
    assert "pricing…" not in reason
    assert "rate limited" in reason


def test_bug2_the_footer_arithmetic_separates_finished_from_outstanding():
    """The bag total's footnote said "2 item(s) still pricing" about two searches
    that had already come back empty. The counts must not be one number."""
    rows = [
        appraise_one(
            _rare_ring(),
            _tier3(state),
            PASSED,
            keep_chaos=DEFAULT_KEEP_CHAOS,
            check_chaos=DEFAULT_CHECK_CHAOS,
        )
        for state in (Tier3.PENDING, Tier3.NO_LISTINGS, Tier3.NO_LISTINGS)
    ]
    bag = BagAppraisal(
        rows, league="Standard", threshold_chaos=20.0, strictness=Strictness.GENEROUS
    )
    assert len(bag.pricing) == 1
    assert len(bag.no_listings) == 2
    payload = bag.to_json()
    assert payload["pricing_count"] == 1
    assert payload["no_listings_count"] == 2
    # Only the outstanding one can still move the figure, so only it makes it a floor.
    assert bag.total_is_floor is True

    settled = BagAppraisal(
        rows[1:], league="Standard", threshold_chaos=20.0, strictness=Strictness.GENEROUS
    )
    assert settled.total_is_floor is False
    assert settled.to_json()["no_listings_count"] == 2


# -- bug 3: quest items were told to vendor ------------------------------------
#
# `The Mortinomicon Exitio Immortalis` normalizes to category `quest`, which is
# correct. `indexable()` is False for it, the gate cannot read it, and it fell
# through to TRASH — whose headline is "vendor". A quest item cannot be traded and
# cannot be vendored; that is not unhelpful advice, it is an impossible instruction.

NOT_LOOT_ITEMS = [
    ("quest", Rarity.QUEST, "The Mortinomicon Exitio Immortalis"),
    ("quest", Rarity.NORMAL, "Book of Skill"),
    ("cosmetic", Rarity.NORMAL, "Arcane Weapon Effect"),
    ("hideout", Rarity.NORMAL, "Ornate Rug"),
]


@pytest.mark.parametrize(("category", "rarity", "name"), NOT_LOOT_ITEMS)
def test_bug3_an_untradeable_item_is_never_trash(category, rarity, name):
    subject = item(name=name, base_type=name, category=category, subcategory=None, rarity=rarity)
    verdict, reason = classify(
        subject,
        valuation(None, name=name),
        NOT_CONSIDERED,
        keep_chaos=DEFAULT_KEEP_CHAOS,
        check_chaos=DEFAULT_CHECK_CHAOS,
    )
    assert verdict is Verdict.NOT_LOOT
    assert verdict is not Verdict.TRASH
    assert verdict is not Verdict.UNPRICEABLE, "its absence from the index is not a hole"
    # And the sentence says nothing about selling it.
    assert "vendor" not in reason or "cannot" in reason


@pytest.mark.parametrize(("category", "rarity", "name"), NOT_LOOT_ITEMS)
def test_bug3_no_threshold_or_gate_can_turn_an_untradeable_item_into_a_loot_verdict(
    category, rarity, name
):
    """Tested before the price branches for exactly this reason: there is no
    valuation, gate result or threshold that should be able to reach past it."""
    subject = item(name=name, base_type=name, category=category, subcategory=None, rarity=rarity)
    for val in (valuation(None), valuation(0.0), valuation(9999.0)):
        for gate in (PASSED, MISSED, NOT_CONSIDERED):
            for keep in (0.0, 20.0, 1e9):
                assert (
                    verdict_of(subject, val, gate, keep=keep) is Verdict.NOT_LOOT
                ), (val, gate, keep)


def test_bug3_a_normal_worthless_item_is_still_trash():
    """The fix must not swallow the case `trash` is for. A white sceptre has no
    price either, and vendoring it is exactly the right advice."""
    sceptre = item(
        name="Driftwood Sceptre",
        base_type="Driftwood Sceptre",
        category="weapon",
        subcategory="sceptres",
        rarity=Rarity.NORMAL,
    )
    assert verdict_of(sceptre, valuation(None), NOT_CONSIDERED) is Verdict.TRASH


def test_bug3_not_loot_rows_are_ranked_last_and_carry_no_money():
    quest = item(
        name="Book of Skill", base_type="Book of Skill", category="quest",
        subcategory=None, rarity=Rarity.QUEST,
    )
    rows = [
        appraise_one(
            quest, valuation(None), NOT_CONSIDERED,
            keep_chaos=DEFAULT_KEEP_CHAOS, check_chaos=DEFAULT_CHECK_CHAOS,
        ),
        appraise_one(
            currency("Divine Orb"), valuation(200.0), NOT_CONSIDERED,
            keep_chaos=DEFAULT_KEEP_CHAOS, check_chaos=DEFAULT_CHECK_CHAOS,
        ),
    ]
    bag = BagAppraisal(
        rows, league="Standard", threshold_chaos=20.0, strictness=Strictness.GENEROUS
    )
    assert bag.ranked()[-1].verdict is Verdict.NOT_LOOT
    assert bag.counts["not_loot"] == 1
    # It contributes nothing and is not counted as a hole either.
    assert bag.total_chaos == pytest.approx(200.0)
    assert bag.unpriceable_stack == 0
