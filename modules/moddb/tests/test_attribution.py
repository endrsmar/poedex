"""Saying "I don't know" out loud.

The brief this module was built to: *where you cannot attribute confidently, say so
in the return type rather than picking the most likely and presenting it as fact.*
These tests are what stop that being a docstring. Each one names a real reason the
answer cannot be pinned down and asserts that no tier comes back.
"""

from __future__ import annotations

from modules.moddb.backend.api import Attribution, ItemMods, Origin


def test_exact_is_the_only_state_that_yields_a_tier(db) -> None:
    match = db.identify("+95 to maximum Life", base_type="Eternal Burgonet", ilvl=86)
    assert match.attribution is Attribution.EXACT
    assert match.tier == 4
    assert match.tier_range == (4, 4)
    assert match.group == "IncreasedLife"
    assert match.note == ""


def test_two_tiers_of_one_group_withhold_the_tier_and_give_the_range(db) -> None:
    """Overlapping bench-craft tiers: the group is a fact, the tier is not.

    ``+25% to Fire Resistance`` sits inside two crafted tiers at once, so there is no
    honest single number. The range is offered, ``tier`` stays ``None``, and a UI
    that wants one number has to decide for itself that it would rather be wrong.
    """
    match = db.identify(
        "+25% to Fire Resistance",
        base_type="Eternal Burgonet",
        ilvl=86,
        origin=Origin.CRAFTED,
    )
    assert match.attribution is Attribution.GROUP
    assert match.tier is None
    assert match.tier_range == (2, 3)
    assert match.group == "FireResistance"
    assert match.describe() == "T2-T3 of 4"
    assert "could have rolled this value" in match.note


def test_two_groups_rendering_one_sentence_is_ambiguous(db) -> None:
    """A real one, off a live item: ``increased Rarity of Items found``.

    Two mod groups produce that sentence on an amulet. Nothing in the text says
    which, so nothing about a tier may be said either — not the tier, not the range,
    not the group, not the ceiling.
    """
    match = db.identify("20% increased Rarity of Items found", base_type="Onyx Amulet", ilvl=86)
    assert match.attribution is Attribution.AMBIGUOUS
    assert match.tier is None
    assert match.tiers is None
    assert match.tier_range is None
    assert match.group is None
    assert match.ceiling is None
    assert match.describe() == "tier unknown"
    assert "2 groups render this text here" in match.note
    # The candidates are still there for anything that wants to reason about them —
    # withholding the *claim* is not the same as withholding the evidence.
    assert len(match.candidates) >= 2


def test_an_unknown_sentence_says_the_artifact_may_be_stale(db) -> None:
    match = db.identify("+3 to Wobbliness", base_type="Coral Ring", ilvl=86)
    assert match.attribution is Attribution.UNKNOWN
    assert match.candidates == ()
    assert match.describe() == "unknown mod"
    assert "league out of date" in match.note


def test_a_value_outside_every_tier_is_unknown_not_the_nearest_tier(db) -> None:
    """The tempting bug: snap to the closest band and call it T1.

    ``+9999 to maximum Life`` is not a roll. Reporting the top tier for it would be
    the same class of error as reporting 10c for a 1c item — plausible, confident,
    wrong, and impossible for the player to check.
    """
    match = db.identify("+9999 to maximum Life", base_type="Legion Plate", ilvl=86)
    assert match.attribution is Attribution.UNKNOWN
    assert match.tier is None
    assert "outside every tier's range" in match.note


def test_a_line_that_is_not_an_affix_says_so(db) -> None:
    implicit = db.identify(
        "24% increased Stun and Block Recovery",
        base_type="Cloth Belt",
        ilvl=69,
        origin=Origin.IMPLICIT,
    )
    assert implicit.attribution is Attribution.UNKNOWN
    assert "not affixes" in implicit.note
    assert not Origin.IMPLICIT.is_affix
    assert not Origin.ENCHANT.is_affix
    assert Origin.EXPLICIT.is_affix and Origin.CRAFTED.is_affix and Origin.FRACTURED.is_affix


def test_whole_item_context_resolves_what_one_line_cannot(db) -> None:
    """A hybrid is ruled out by the line that is *not* on the item.

    ``+26 to Armour`` alone could be the armour prefix or the armour-and-life hybrid.
    On an item with no life line, the hybrid is impossible — and that is a deduction,
    not a preference, so the tier is allowed.
    """
    alone = db.identify("+26 to Armour", base_type="Iron Hat", ilvl=41)
    assert alone.attribution is Attribution.AMBIGUOUS

    in_context = db.report(
        ItemMods(
            base_type="Iron Hat",
            ilvl=41,
            rarity="rare",
            explicit=["+26 to Armour", "8% increased Rarity of Items found"],
        )
    )
    armour = in_context.matches[0]
    assert armour.attribution is Attribution.EXACT
    assert armour.group == "BaseLocalDefences"


def test_context_narrows_but_never_eliminates(db) -> None:
    """If every candidate wants a line the item lacks, the item is under-described.

    That is much more likely than the mod being impossible — a flask's immunity line
    is easy for a caller to forget to pass — so the unfiltered answer comes back with
    a note, rather than an "unknown" that is more certain of its ignorance than the
    evidence supports.
    """
    report = db.report(
        ItemMods(
            base_type="Basalt Flask",
            ilvl=65,
            rarity="magic",
            explicit=["43% less Duration"],
        )
    )
    match = report.matches[0]
    assert match.attribution is Attribution.AMBIGUOUS
    assert match.candidates
    assert "attributed without that check" in match.note


def test_top_group_survives_an_uncertain_tier(db) -> None:
    """The question a gate asks is "is this the best it gets", not "which row".

    If every surviving candidate is T1 then the roll is top-tier whichever of them it
    was, and that claim is safe even when the tier is not exactly known.
    """
    match = db.identify("+189 to maximum Life", base_type="Legion Plate", ilvl=86)
    assert match.top_group
    mediocre = db.identify("+40 to maximum Life", base_type="Legion Plate", ilvl=86)
    assert not mediocre.top_group


def test_is_confident_is_the_property_a_caller_should_branch_on(db) -> None:
    assert Attribution.EXACT.is_confident
    assert Attribution.GROUP.is_confident
    assert not Attribution.AMBIGUOUS.is_confident
    assert not Attribution.UNKNOWN.is_confident


def test_every_match_serializes(db) -> None:
    match = db.identify("+95 to maximum Life", base_type="Coral Ring", ilvl=86)
    payload = match.to_json()
    assert payload["attribution"] == "exact"
    assert payload["tier"] == match.tier
    assert payload["description"] == match.describe()
    assert payload["ceiling"] == match.ceiling
    unknown = db.identify("+3 to Wobbliness", base_type="Coral Ring", ilvl=86).to_json()
    assert unknown["tier"] is None and unknown["group"] is None
