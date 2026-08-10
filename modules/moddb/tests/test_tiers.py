"""Tier resolution, which is the whole reason this module exists.

`appraisal`'s gate scores ``+80 to maximum Life`` identically on a helmet and on a
body armour and says so in its own docstring. These tests are the demonstration that
those are different facts.
"""

from __future__ import annotations

import pytest

from modules.moddb.backend.api import Affix, Attribution, Origin


class TestTheSameTextIsADifferentTierOnDifferentBases:
    """One roll, six bases, six answers. This is the finding, not a detail.

    ``+95 to maximum Life`` is T4 of 10 on a helmet and T7 of 13 on a body armour,
    because body armours get three life tiers nothing else does — up to ``+189``.
    A single threshold cannot express that, and no amount of tuning one will.
    """

    @pytest.mark.parametrize(
        ("base", "tier", "tiers", "ceiling"),
        [
            ("Eternal Burgonet", 4, 10, 144.0),
            ("Legion Plate", 7, 13, 189.0),
            ("Coral Ring", 2, 8, 114.0),
            ("Onyx Amulet", 3, 9, 129.0),
            ("Cloth Belt", 4, 10, 144.0),
        ],
    )
    def test_life(self, db, base: str, tier: int, tiers: int, ceiling: float) -> None:
        match = db.identify("+95 to maximum Life", base_type=base, ilvl=86)
        assert match.attribution is Attribution.EXACT
        assert (match.tier, match.tiers) == (tier, tiers)
        assert match.ceiling == ceiling
        assert match.describe() == f"T{tier} of {tiers}"

    def test_the_ceiling_is_what_replaces_a_flat_threshold(self, db) -> None:
        """``gate.py`` calls 80 life "good". Here is what 80 is worth, per base."""
        body = db.ceiling("+80 to maximum Life", base_type="Legion Plate", ilvl=86)
        helmet = db.ceiling("+80 to maximum Life", base_type="Eternal Burgonet", ilvl=86)
        ring = db.ceiling("+80 to maximum Life", base_type="Coral Ring", ilvl=86)
        assert body > helmet > ring
        assert 80 / body == pytest.approx(0.423, abs=0.01)
        assert 80 / ring == pytest.approx(0.702, abs=0.01)


def test_top_tier_is_only_claimed_when_it_is_true(db) -> None:
    top = db.identify("+189 to maximum Life", base_type="Legion Plate", ilvl=86)
    assert top.tier == 1
    assert top.top_tier and top.top_group
    ordinary = db.identify("+95 to maximum Life", base_type="Legion Plate", ilvl=86)
    assert not ordinary.top_tier and not ordinary.top_group


def test_a_mod_that_cannot_roll_on_a_base_is_not_given_a_tier(db) -> None:
    """``spawn_weight: 0`` — the rule that stops a ring rolling weapon mods.

    Local physical damage is a weapon mod and resistances are not weapon mods, and
    both facts come out of the same table. Getting this wrong would not look like a
    bug: it would look like a ring with a surprisingly good damage roll.
    """
    on_a_bow = db.identify("145% increased Physical Damage", base_type="Spine Bow", ilvl=85)
    assert on_a_bow.attribution is Attribution.EXACT
    assert on_a_bow.group == "LocalPhysicalDamagePercent"

    on_a_ring = db.identify("145% increased Physical Damage", base_type="Coral Ring", ilvl=85)
    assert on_a_ring.attribution is Attribution.UNKNOWN
    assert on_a_ring.tier is None
    assert "nothing that can spawn here" in on_a_ring.note

    assert db.identify(
        "+41% to Fire Resistance", base_type="Spine Bow", ilvl=85
    ).attribution is Attribution.UNKNOWN


def test_the_tier_ladder_is_the_bases_ladder_not_the_groups(db) -> None:
    """Two bases, one group, different ladder lengths — because the tiers differ."""
    body = db.identify("+154 to maximum Life", base_type="Legion Plate", ilvl=84)
    assert body.tiers == 13
    helmet = db.identify("+95 to maximum Life", base_type="Eternal Burgonet", ilvl=86)
    assert helmet.tiers == 10
    # And the extra three are real: a helmet cannot reach them at all.
    assert db.identify(
        "+154 to maximum Life", base_type="Eternal Burgonet", ilvl=86
    ).attribution is Attribution.UNKNOWN


def test_item_level_gates_the_tiers_that_could_have_rolled(db) -> None:
    """A level-30 item cannot carry a mod that needs 86, whatever the number says."""
    high = db.identify("+189 to maximum Life", base_type="Legion Plate", ilvl=86)
    assert high.attribution is Attribution.EXACT
    low = db.identify("+189 to maximum Life", base_type="Legion Plate", ilvl=40)
    assert low.attribution is Attribution.UNKNOWN


def test_prefix_and_suffix_come_from_the_data(db) -> None:
    assert db.identify("+95 to maximum Life", base_type="Coral Ring", ilvl=86).affix is Affix.PREFIX
    assert db.identify(
        "+41% to Fire Resistance", base_type="Coral Ring", ilvl=86
    ).affix is Affix.SUFFIX


def test_an_unknown_base_is_named_as_such(db) -> None:
    match = db.identify("+95 to maximum Life", base_type="Chaos Orb", ilvl=0)
    assert match.attribution is Attribution.UNKNOWN
    assert "not a base that rolls affixes" in match.note
    assert db.base("Chaos Orb") is None


@pytest.mark.parametrize(
    ("base", "top"),
    [
        ("Legion Plate", 86),
        ("Vaal Regalia", 86),
        ("Spine Bow", 86),
        ("Onyx Amulet", 86),
        ("Cloth Belt", 86),
        # 85, not 86 — and the gate flags every ilvl-86 accessory and helmet as
        # special. On these three that flag buys nothing 85 did not already.
        ("Coral Ring", 85),
        ("Eternal Burgonet", 85),
        ("Hubris Circlet", 85),
        # A flask's mods *do* scale with item level, which is the opposite of what
        # ``ILVL86_BASE_CATEGORIES``' exclusion of flasks asserts.
        ("Basalt Flask", 85),
    ],
)
def test_bases_answer_the_ilvl_question_the_gate_hardcodes(db, base: str, top: int) -> None:
    """``ILVL86_BASE_CATEGORIES`` is a guess about categories. This is the data."""
    info = db.base(base)
    assert info.top_affix_level == top
    assert info.fully_rolled(top)
    assert not info.fully_rolled(top - 1)
    assert info.ilvl_matters


def test_a_jewel_has_no_item_level_tiers_at_all(db) -> None:
    """The one case ``ILVL86_BASE_CATEGORIES`` gets right, for the right reason here.

    Every jewel affix in the game is ``required_level: 1``. So a jewel is fully
    rolled the moment it drops, and item level is not a fact about it — which is
    what ``ilvl_matters`` returning ``False`` means, derived rather than listed.
    """
    jewel = db.base("Cobalt Jewel")
    assert jewel.top_affix_level == 1
    assert not jewel.ilvl_matters
    assert jewel.fully_rolled(1)


def test_the_top_tier_base_marker_comes_from_ggg(db) -> None:
    """``HIGH_VALUE_BASES`` is twenty-six names typed from memory. This is a tag."""
    assert db.base("Hubris Circlet").is_top_tier
    assert db.base("Vaal Regalia").is_top_tier
    assert db.base("Eternal Burgonet").is_top_tier
    assert not db.base("Iron Hat").is_top_tier
    assert not db.base("Coral Ring").is_top_tier


def test_groups_lists_what_a_base_can_actually_roll(db) -> None:
    ring = db.groups("Coral Ring")
    bow = db.groups("Spine Bow")
    assert "IncreasedLife" in ring
    assert "LocalPhysicalDamagePercent" in bow
    assert "LocalPhysicalDamagePercent" not in ring
    assert db.groups("Coral Ring", affix=Affix.PREFIX) != db.groups(
        "Coral Ring", affix=Affix.SUFFIX
    )
    assert db.groups("Chaos Orb") == ()


def test_a_crafted_line_is_never_confused_with_an_explicit_one(db) -> None:
    """The item endpoints already separate them, so the database uses that for free."""
    crafted = db.identify(
        "+25% to Fire Resistance", base_type="Eternal Burgonet", ilvl=86, origin=Origin.CRAFTED
    )
    explicit = db.identify("+25% to Fire Resistance", base_type="Eternal Burgonet", ilvl=86)
    assert crafted.attribution.is_confident and explicit.attribution.is_confident
    assert crafted.tiers != explicit.tiers
    assert crafted.ceiling == 35.0
    assert explicit.ceiling == 48.0
