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
from modules.prices.backend.api import BagValuation, Price, PriceSource, Valuation
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


def test_all_four_verdicts_are_always_present_in_the_tally():
    """A tally built by counting what is there loses the states that are not, and a
    panel that drops the `unpriceable` line when a bag happens to have none teaches
    the player it does not exist."""
    empty = BagAppraisal(
        [], league="Standard", threshold_chaos=20.0, strictness=Strictness.GENEROUS
    )
    assert empty.counts == {"keep": 0, "check": 0, "trash": 0, "unpriceable": 0}


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
