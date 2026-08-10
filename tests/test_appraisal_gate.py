"""Tier 2 — the strictness-parameterized gate (SPEC §5.2).

The load-bearing property is not "the gate flags good items"; it is that the two
strictness levels **disagree, in the specified direction, on the same items**. A gate
whose two settings behave identically would pass every test that only checked one of
them, and it would be useless: the whole reason for the parameter is that the bag and
the stash want opposite errors.

So the divergence tests below run one list of items through both levels and assert
the partition, rather than testing each level against its own convenient example.
"""

from __future__ import annotations

import pytest

from modules.appraisal.backend.api import Strictness
from modules.appraisal.backend.gate import (
    HIGH_VALUE_BASES,
    MOD_GROUPS,
    evaluate,
    gate_applies,
    mod_hits,
)
from modules.poeapi.backend.api import Grid, Location, Mods, NormalizedItem, Rarity, Sockets, Source


def item(
    *,
    name: str = "Test Item",
    base_type: str = "Coral Ring",
    category: str = "accessory",
    subcategory: str | None = "ring",
    rarity: Rarity = Rarity.RARE,
    ilvl: int = 70,
    links: int = 0,
    influences: list[str] | None = None,
    fractured: bool = False,
    synthesised: bool = False,
    identified: bool = True,
    explicit: list[str] | None = None,
    implicit: list[str] | None = None,
    fractured_mods: list[str] | None = None,
    veiled: list[str] | None = None,
    **extra,
) -> NormalizedItem:
    return NormalizedItem(
        uid=f"uid-{name}-{base_type}-{ilvl}",
        name=name,
        base_type=base_type,
        category=category,
        subcategory=subcategory,
        rarity=rarity,
        ilvl=ilvl,
        grid=Grid(),
        sockets=Sockets(count=links, links=links),
        fractured=fractured,
        synthesised=synthesised,
        identified=identified,
        influences=influences or [],
        mods=Mods(
            explicit=explicit or [],
            implicit=implicit or [],
            fractured=fractured_mods or [],
            veiled=veiled or [],
        ),
        location=Location(source=Source.BAG, slot="MainInventory"),
        **extra,
    )


def signals(result) -> set[str]:
    return {signal.name for signal in result.signals}


# -- what the gate is even asked about -----------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"rarity": Rarity.RARE, "category": "accessory"}, True),
        ({"rarity": Rarity.MAGIC, "category": "weapon"}, True),
        ({"rarity": Rarity.RARE, "category": "jewel"}, True),
        # Bulk. It has a price; a heuristic opinion about it would be noise.
        ({"rarity": Rarity.CURRENCY, "category": "currency"}, False),
        ({"rarity": Rarity.DIVINATION, "category": "card"}, False),
        ({"rarity": Rarity.UNIQUE, "category": "armour"}, False),
        ({"rarity": Rarity.NORMAL, "category": "map"}, False),
        ({"rarity": Rarity.GEM, "category": "gem"}, False),
    ],
)
def test_only_rares_and_magic_gear_reach_tier_two(kwargs, expected):
    assert gate_applies(item(**kwargs)) is expected


def test_an_ungated_item_reports_that_it_was_never_considered():
    """`considered=False` is not the same as `passed=False`, and a surface that
    conflated them would say "the gate rejected your Chaos Orb"."""
    result = evaluate(item(rarity=Rarity.CURRENCY, category="currency"))
    assert result.considered is False
    assert result.passed is False
    assert result.signals == []


# -- the hard requirements, one at a time (SPEC §5.2) --------------------------


HARD_CASES = [
    ("influence", {"influences": ["shaper"]}),
    ("fractured", {"fractured": True}),
    ("fractured", {"fractured_mods": ["+48 to maximum Life"]}),
    ("synthesised", {"synthesised": True}),
    ("six_link", {"links": 6, "category": "armour", "subcategory": "body_armour"}),
    ("ilvl_86", {"ilvl": 86}),
    ("high_value_base", {"base_type": "Vermillion Ring"}),
]


@pytest.mark.parametrize(("expected", "kwargs"), HARD_CASES)
def test_each_hard_requirement_passes_both_gates(expected, kwargs):
    for level in Strictness:
        result = evaluate(item(**kwargs), strictness=level)
        assert result.passed, f"{expected} should pass at {level.value}"
        assert expected in signals(result)
        assert any(signal.hard for signal in result.signals)


def test_a_five_link_is_not_a_six_link():
    assert not evaluate(
        item(links=5, category="armour", subcategory="body_armour"), strictness=Strictness.STRICT
    ).passed


def test_ilvl_85_is_not_ilvl_86():
    assert "ilvl_86" not in signals(evaluate(item(ilvl=85), strictness=Strictness.STRICT))
    assert "ilvl_86" in signals(evaluate(item(ilvl=86), strictness=Strictness.STRICT))


def test_ilvl_86_only_counts_on_a_base_where_it_matters():
    """SPEC §5.2's wording, taken literally. A jewel has no item-level tiers worth
    the name, so an ilvl-86 jewel is not evidence of anything."""
    jewel = item(base_type="Cobalt Jewel", category="jewel", subcategory=None, ilvl=86)
    assert "ilvl_86" not in signals(evaluate(jewel, strictness=Strictness.STRICT))
    ring = item(base_type="Coral Ring", category="accessory", subcategory="ring", ilvl=86)
    assert "ilvl_86" in signals(evaluate(ring, strictness=Strictness.STRICT))


def test_the_allowlist_is_case_insensitive_and_extensible():
    assert evaluate(item(base_type="vermillion ring"), strictness=Strictness.STRICT).passed
    unknown = item(base_type="Sadist Garb", category="armour", subcategory="body_armour")
    assert not evaluate(unknown, strictness=Strictness.STRICT).passed
    extended = evaluate(
        unknown, strictness=Strictness.STRICT, allowlist={*HIGH_VALUE_BASES, "Sadist Garb"}
    )
    assert "high_value_base" in signals(extended)


# -- the divergence. This is the point of the parameter. -----------------------


GENEROUS_ONLY = {
    "good rolls, worthless base": item(
        name="Loath Grip",
        base_type="Coral Ring",
        ilvl=81,
        explicit=[
            "+79 to maximum Life",
            "+41% to Fire Resistance",
            "+39% to Cold Resistance",
            "+38% to Lightning Resistance",
        ],
    ),
    "the false-positive engine": item(
        name="Woe Edge",
        base_type="Rusted Sword",
        category="weapon",
        subcategory="sword",
        ilvl=62,
        explicit=["+23 to maximum Life", "11% increased Attack Speed"],
    ),
    "unidentified, ordinary base": item(
        name="",
        base_type="Iron Hat",
        category="armour",
        subcategory="helmet",
        ilvl=72,
        identified=False,
    ),
    "veiled": item(base_type="Coral Ring", ilvl=80, veiled=["Prefix Unveil"]),
}

BOTH_REJECT = {
    "nothing at all": item(
        name="Dread Guard",
        base_type="Iron Hat",
        category="armour",
        subcategory="helmet",
        ilvl=41,
        explicit=["+26 to Armour", "8% increased Rarity of Items Found"],
    ),
    "magic flask": item(
        name="",
        base_type="Diamond Flask",
        category="flask",
        subcategory=None,
        rarity=Rarity.MAGIC,
        ilvl=73,
    ),
}


@pytest.mark.parametrize("label", sorted(GENEROUS_ONLY))
def test_the_generous_gate_catches_what_the_strict_gate_drops(label):
    subject = GENEROUS_ONLY[label]
    assert evaluate(subject, strictness=Strictness.GENEROUS).passed, label
    assert not evaluate(subject, strictness=Strictness.STRICT).passed, label


@pytest.mark.parametrize("label", sorted(BOTH_REJECT))
def test_neither_gate_flags_an_item_with_nothing_on_it(label):
    subject = BOTH_REJECT[label]
    assert not evaluate(subject, strictness=Strictness.GENEROUS).passed, label
    assert not evaluate(subject, strictness=Strictness.STRICT).passed, label


def test_the_same_item_list_partitions_differently_at_each_strictness():
    """One list, both gates, and the sets are nested and *not equal*.

    Strict ⊂ generous is the invariant that makes "same code, a parameter" true:
    the strict gate is the generous gate with the soft signals removed, so anything
    strict accepts generous must accept too.
    """
    items = [
        *GENEROUS_ONLY.values(),
        *BOTH_REJECT.values(),
        item(name="Behemoth Bind", base_type="Leather Belt", influences=["shaper"]),
        item(name="Gloom Coil", base_type="Two-Stone Ring", ilvl=86),
    ]
    generous = {i.uid for i in items if evaluate(i, strictness=Strictness.GENEROUS).passed}
    strict = {i.uid for i in items if evaluate(i, strictness=Strictness.STRICT).passed}
    assert strict < generous, "strict must be a proper subset of generous"
    assert len(generous) - len(strict) == len(GENEROUS_ONLY)


def test_the_strict_gate_reports_only_hard_signals():
    """SPEC §5.2: the "desirable mod group present" signal is dropped at strict.
    Asserted on an item that has *both* a hard requirement and soft ones, because
    that is where a sloppy implementation leaks."""
    subject = item(
        base_type="Vermillion Ring",
        ilvl=86,
        influences=["hunter"],
        explicit=["+79 to maximum Life", "+45% to Fire Resistance"],
    )
    strict = evaluate(subject, strictness=Strictness.STRICT)
    generous = evaluate(subject, strictness=Strictness.GENEROUS)
    assert all(signal.hard for signal in strict.signals)
    assert not all(signal.hard for signal in generous.signals)
    assert signals(strict) < signals(generous)


# -- the roll thresholds, which are an approximation and say so -----------------


def test_a_roll_over_its_threshold_is_a_stronger_signal_than_mere_presence():
    over = evaluate(item(explicit=["+95 to maximum Life"]))
    under = evaluate(item(explicit=["+21 to maximum Life"]))
    assert "roll:life" in signals(over)
    assert "mod_group" in signals(under)
    assert "roll:life" not in signals(under)


def test_resistances_are_summed_and_everything_else_takes_the_best_roll():
    """Three 40% resistances is a real suffix set; three separate 40s compared one
    at a time against a 110 threshold would find nothing."""
    resists = item(
        explicit=[
            "+40% to Fire Resistance",
            "+38% to Cold Resistance",
            "+37% to Lightning Resistance",
        ]
    )
    hits = {
        group.name: (value, sources)
        for group, value, sources in mod_hits(resists, require_threshold=False)
    }
    assert hits["resistances"][0] == pytest.approx(115.0)
    # ...and every line that contributed is carried, because a summed roll has no
    # single mod behind it and tier 3 must not put a floor on one of them.
    assert len(hits["resistances"][1]) == 3

    # Life is not summed: two mediocre life rolls are not one good one.
    lives = item(explicit=["+45 to maximum Life", "+44 to maximum Life"])
    hits = {
        group.name: (value, sources)
        for group, value, sources in mod_hits(lives, require_threshold=False)
    }
    assert hits["life"][0] == pytest.approx(45.0)
    # The line that actually rolled it — not both, and not the wrong one.
    assert hits["life"][1] == ["+45 to maximum Life"]


def test_a_flat_damage_range_is_read_as_its_midpoint():
    hit = {
        group.name: value
        for group, value, _sources in mod_hits(
            item(explicit=["Adds 12 to 30 Physical Damage"]), require_threshold=False
        )
    }
    assert hit["added_physical"] == pytest.approx(21.0)


def test_an_unidentified_item_produces_no_mod_signals_at_all():
    """It cannot: there are no mods to read. The generous gate says exactly that
    rather than silently finding nothing and calling it trash."""
    blind = item(identified=False, explicit=["+95 to maximum Life"])
    result = evaluate(blind)
    assert signals(result) == {"unidentified"}


def test_every_mod_group_pattern_captures_a_number():
    """A regex with no numeric group would silently never fire — the failure mode
    that makes a heuristic look conservative instead of broken."""
    for group in MOD_GROUPS:
        assert group.pattern.groups >= 1, group.name
        assert group.threshold > 0, group.name


def test_mod_groups_read_implicit_crafted_and_fractured_text_too():
    """A crafted +life is still +life. Reading only `explicit` would miss the mod
    the player paid a bench craft for."""
    subject = item(
        base_type="Coral Ring",
        implicit=["+95 to maximum Life"],
    )
    assert "roll:life" in signals(evaluate(subject))
    subject = item(base_type="Coral Ring", fractured_mods=["+95 to maximum Life"])
    assert "roll:life" in signals(evaluate(subject))
