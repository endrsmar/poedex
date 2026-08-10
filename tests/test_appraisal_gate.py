"""Tier 2 — the highlighter, built on `moddb` (IMPLEMENTATION-PLAN §5b).

These tests run against the **real committed mod database**, not a stand-in. That is
deliberate and it is the whole point of the phase: the claims this file makes —
``+95 to maximum Life`` is T4 on a helmet and T7 on a body armour, a Hubris Circlet
tops out at affix level 85 — are claims about the game, and a fake database would let
them be whatever the test wanted. The artifact is offline, committed, and 8 ms to
load, so there is no cost to being honest here.

Two properties are load-bearing and each has its own section:

* **The two strictness levels disagree, in the specified direction, on the same
  items.** A parameter whose settings behave identically is a parameter nobody needs.
* **Nothing shows a tier `moddb` did not assert.** The old file's fourteen regexes
  answered every question; this one refuses several, and the refusals are tested as
  carefully as the answers.
"""

from __future__ import annotations

import pytest

from modules.appraisal.backend.api import Strictness
from modules.appraisal.backend.gate import (
    ILVL86,
    SOUGHT_AFTER_BASES,
    describe_report,
    evaluate,
    gate_applies,
    high_tier,
    item_mods,
    report_for,
)
from modules.moddb.backend.database import load as load_moddb
from modules.poeapi.backend.api import Grid, Location, Mods, NormalizedItem, Rarity, Sockets, Source


@pytest.fixture(scope="module")
def moddb():
    """The real artifact, once for the module. Immutable, and every test only reads."""
    return load_moddb()


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
    crafted: list[str] | None = None,
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
            crafted=crafted or [],
            fractured=fractured_mods or [],
            veiled=veiled or [],
        ),
        location=Location(source=Source.BAG, slot="MainInventory"),
        **extra,
    )


def signals(result) -> set[str]:
    return {signal.name for signal in result.signals}


def details(result) -> str:
    return result.summary


# -- what the highlighter is even asked about ----------------------------------


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


def test_an_ungated_item_reports_that_it_was_never_considered(moddb):
    """`considered=False` is not the same as `passed=False`, and a surface that
    conflated them would say "the gate rejected your Chaos Orb"."""
    result = evaluate(item(rarity=Rarity.CURRENCY, category="currency"), moddb=moddb)
    assert result.considered is False
    assert result.passed is False
    assert result.signals == []


# -- the four criteria (IMPLEMENTATION-PLAN §5b) -------------------------------


def test_criterion_valuable_base_at_high_ilvl(moddb):
    """GGG's own ``top_tier_base_item_type`` tag, **and** the item level to use it.

    A Hubris Circlet carries the tag; a Coral Ring does not. Neither is a highlight
    on its own — the tag on an ilvl-1 drop buys nothing, and a high item level on an
    ordinary base buys nothing either.
    """
    top = item(base_type="Hubris Circlet", category="armour", subcategory="helmet", ilvl=85)
    assert "top_tier_base" in signals(evaluate(top, strictness=Strictness.STRICT, moddb=moddb))

    low = item(base_type="Hubris Circlet", category="armour", subcategory="helmet", ilvl=70)
    assert "top_tier_base" not in signals(evaluate(low, strictness=Strictness.STRICT, moddb=moddb))

    ordinary = item(base_type="Coral Ring", ilvl=86)
    assert "top_tier_base" not in signals(
        evaluate(ordinary, strictness=Strictness.STRICT, moddb=moddb)
    )


def test_criterion_high_tier_roll_is_the_top_tier_on_this_base(moddb):
    """T1 *here*, never a threshold. ``+130`` life is T1 on a helmet; ``+95`` is T4."""
    great = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        explicit=["+130 to maximum Life"],
    )
    result = evaluate(great, strictness=Strictness.STRICT, moddb=moddb)
    assert any(name.startswith("tier:") for name in signals(result))
    assert "T1 of 10" in details(result)

    ordinary = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        explicit=["+95 to maximum Life"],
    )
    assert not any(
        name.startswith("tier:")
        for name in signals(evaluate(ordinary, strictness=Strictness.STRICT, moddb=moddb))
    )


def test_criterion_six_link(moddb):
    six = item(base_type="Astral Plate", category="armour", subcategory="body_armour", links=6)
    assert "six_link" in signals(evaluate(six, strictness=Strictness.STRICT, moddb=moddb))
    five = item(base_type="Astral Plate", category="armour", subcategory="body_armour", links=5)
    assert "six_link" not in signals(evaluate(five, strictness=Strictness.STRICT, moddb=moddb))


def test_criterion_influence_mod_not_merely_an_influenced_item(moddb):
    """The distinction the old gate could not draw, and the one that matters.

    Most influenced items carry no influence mod at all — the tag was applied and
    never exploited — so flagging the tag puts a highlight on nothing. This asks
    whether a mod **from an influence pool** is on the item.
    """
    exploited = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        influences=["warlord"],
        explicit=["Nearby Enemies have -9% to Fire Resistance"],
    )
    result = evaluate(exploited, strictness=Strictness.STRICT, moddb=moddb)
    assert "influence_mod" in signals(result)
    assert "warlord mod" in details(result)

    tagged_only = item(
        base_type="Coral Ring",
        ilvl=70,
        influences=["warlord"],
        explicit=["+45 to maximum Life"],
    )
    assert "influence_mod" not in signals(
        evaluate(tagged_only, strictness=Strictness.STRICT, moddb=moddb)
    )


# -- the two facts the deleted constants got wrong -----------------------------


def test_the_same_life_roll_is_a_different_tier_on_a_helmet_and_a_body_armour(moddb):
    """One regex and one threshold could not say this, which is why they are gone.

    ``+95 to maximum Life`` is **T4 of 10** on a Hubris Circlet and **T7 of 13** on
    an Astral Plate, and the ceilings are 144 and 189. The old ``MOD_GROUPS`` scored
    both as ``max life 95 >= 80``.
    """
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
    on_helmet = report_for(helmet, moddb).matches[0]
    on_body = report_for(body, moddb).matches[0]

    assert (on_helmet.tier, on_helmet.tiers) == (4, 10)
    assert (on_body.tier, on_body.tiers) == (7, 13)
    assert on_helmet.ceiling == 144.0
    assert on_body.ceiling == 189.0


def test_an_ilvl_85_helmet_is_already_fully_rolled(moddb):
    """The old ``ilvl >= 86`` was wrong on a third of the gear it fired for.

    A Hubris Circlet's highest affix needs item level **85**, so 86 buys nothing 85
    did not already. The highlight fires at 85 and does not wait for 86.
    """
    base = moddb.base("Hubris Circlet")
    assert base.top_affix_level == 85
    assert base.fully_rolled(85) is True

    at85 = item(base_type="Hubris Circlet", category="armour", subcategory="helmet", ilvl=85)
    result = evaluate(at85, strictness=Strictness.STRICT, moddb=moddb)
    assert "top_tier_base" in signals(result)
    assert "ilvl 85/85" in details(result)

    at84 = item(base_type="Hubris Circlet", category="armour", subcategory="helmet", ilvl=84)
    assert "top_tier_base" not in signals(evaluate(at84, strictness=Strictness.STRICT, moddb=moddb))


def test_a_body_armour_really_does_wait_for_86(moddb):
    """The other half of the same claim: 85 is not universal either."""
    assert moddb.base("Astral Plate").top_affix_level == 86
    at85 = item(base_type="Astral Plate", category="armour", subcategory="body_armour", ilvl=85)
    at86 = item(base_type="Astral Plate", category="armour", subcategory="body_armour", ilvl=86)
    assert "top_tier_base" not in signals(evaluate(at85, strictness=Strictness.STRICT, moddb=moddb))
    assert "top_tier_base" in signals(evaluate(at86, strictness=Strictness.STRICT, moddb=moddb))


def test_the_seven_bases_ggg_marks_are_gone_from_the_opinion_list(moddb):
    """What survives subtracting GGG's tag is an opinion, and only an opinion.

    Seven of the twenty-six hand-typed bases carry ``top_tier_base_item_type``;
    :data:`SOUGHT_AFTER_BASES` is the other nineteen. A name appearing in both would
    mean the factual claim and the market claim had been merged again.
    """
    for name in SOUGHT_AFTER_BASES:
        info = moddb.base(name)
        assert info is not None, name
        assert not info.is_top_tier, f"{name} is answered by the tag; drop it from the opinion"
    assert len(SOUGHT_AFTER_BASES) == 19


def test_the_opinion_list_is_soft_and_the_strict_gate_is_entirely_factual(moddb):
    """A market opinion may not be a hard requirement. This is the property that
    makes "the strict gate claims only facts" checkable rather than aspirational."""
    sought = item(base_type="Vermillion Ring", ilvl=85)
    assert "sought_after_base" in signals(evaluate(sought, moddb=moddb))
    assert not evaluate(sought, strictness=Strictness.STRICT, moddb=moddb).passed


# -- refusing to claim a tier --------------------------------------------------


def test_an_ambiguous_line_is_never_a_high_tier_roll(moddb):
    """`moddb` will not say which mod produced ``10% increased Rarity of Items
    found`` on a helmet — several ladders reach it — so neither will the
    highlighter. Nothing may be claimed from a ladder nobody picked."""
    subject = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        explicit=["10% increased Rarity of Items found"],
    )
    match = report_for(subject, moddb).matches[0]
    assert match.attribution.value == "ambiguous"
    assert match.tier is None
    assert high_tier(match, strictness=Strictness.GENEROUS) is False
    assert not any(name.startswith("tier:") for name in signals(evaluate(subject, moddb=moddb)))


def test_the_note_says_how_much_of_the_item_was_readable(moddb):
    """"Nothing high-tier here" and "three lines came back unknown" are different
    answers, and a highlighter that cannot tell them apart says nothing by saying
    nothing."""
    subject = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        explicit=["+95 to maximum Life", "+26 to Armour"],
    )
    note = describe_report(report_for(subject, moddb))
    assert "1/2 lines attributed" in note
    assert "1 not in the database" in note


def test_without_a_database_the_factual_signals_still_fire(moddb):
    """A missing artifact must degrade the *tier* claims and nothing else."""
    six = item(
        base_type="Astral Plate",
        category="armour",
        subcategory="body_armour",
        links=6,
        ilvl=86,
        explicit=["+130 to maximum Life"],
    )
    blind = evaluate(six, strictness=Strictness.STRICT, moddb=None)
    assert "six_link" in signals(blind)
    assert not any(name.startswith("tier:") for name in signals(blind))
    assert describe_report(None) == "no mod database — tiers unavailable"


def test_an_unknown_base_falls_back_to_the_ilvl_86_constant(moddb):
    """The only thing 86 is still used for, and it is used as a fallback, not a rule."""
    assert ILVL86 == 86
    invented = item(base_type="Grand Regalia of Nowhere", category="armour", ilvl=86)
    # No BaseInfo, so no `is_top_tier` and therefore no highlight — but the fallback
    # is exercised through `_fully_rolled` and must not raise.
    assert evaluate(invented, strictness=Strictness.STRICT, moddb=moddb).considered


# -- the divergence. This is the point of the parameter. -----------------------


def generous_only(moddb) -> dict[str, NormalizedItem]:
    return {
        "near-top roll on a long ladder": item(
            name="Loath Grip",
            base_type="Siege Helmet",
            category="armour",
            subcategory="helmet",
            ilvl=86,
            explicit=["+120 to maximum Life"],
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
        "sought-after base, an opinion": item(base_type="Stygian Vise", category="accessory",
                                              subcategory="belt", ilvl=86),
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


def test_the_generous_gate_catches_what_the_strict_gate_drops(moddb):
    for label, subject in generous_only(moddb).items():
        assert evaluate(subject, strictness=Strictness.GENEROUS, moddb=moddb).passed, label
        assert not evaluate(subject, strictness=Strictness.STRICT, moddb=moddb).passed, label


@pytest.mark.parametrize("label", sorted(BOTH_REJECT))
def test_neither_gate_flags_an_item_with_nothing_on_it(label, moddb):
    subject = BOTH_REJECT[label]
    assert not evaluate(subject, strictness=Strictness.GENEROUS, moddb=moddb).passed, label
    assert not evaluate(subject, strictness=Strictness.STRICT, moddb=moddb).passed, label


def test_the_same_item_list_partitions_differently_at_each_strictness(moddb):
    """One list, both gates, and the sets are nested and *not equal*.

    Strict ⊂ generous is the invariant that makes "same code, a parameter" true.
    """
    soft = generous_only(moddb)
    items = [
        *soft.values(),
        *BOTH_REJECT.values(),
        item(
            name="Behemoth Bind",
            base_type="Astral Plate",
            category="armour",
            subcategory="body_armour",
            links=6,
        ),
        item(
            name="Gloom Coil",
            base_type="Hubris Circlet",
            category="armour",
            subcategory="helmet",
            ilvl=86,
        ),
    ]
    generous = {i.uid for i in items if evaluate(i, moddb=moddb).passed}
    strict = {i.uid for i in items if evaluate(i, strictness=Strictness.STRICT, moddb=moddb).passed}
    assert strict < generous, "strict must be a proper subset of generous"
    assert len(generous) - len(strict) == len(soft)


def test_the_strict_gate_reports_only_hard_signals(moddb):
    """Asserted on an item that has *both* hard and soft signals, because that is
    where a sloppy implementation leaks."""
    subject = item(
        base_type="Hubris Circlet",
        category="armour",
        subcategory="helmet",
        ilvl=85,
        veiled=["Prefix Unveil"],
        explicit=["+130 to maximum Life"],
    )
    strict = evaluate(subject, strictness=Strictness.STRICT, moddb=moddb)
    generous = evaluate(subject, moddb=moddb)
    assert all(signal.hard for signal in strict.signals)
    assert not all(signal.hard for signal in generous.signals)
    assert signals(strict) < signals(generous)


def test_an_unidentified_item_produces_no_mod_signals_at_all(moddb):
    """It cannot: there are no mods to read. The generous gate says exactly that
    rather than silently finding nothing and calling it trash."""
    blind = item(identified=False, explicit=["+130 to maximum Life"])
    assert signals(evaluate(blind, moddb=moddb)) == {"unidentified"}


# -- the translation into `moddb`'s input shape --------------------------------


def test_item_mods_carries_every_affix_origin_and_no_placeholders():
    """A crafted +life is still +life. ``veiled`` is excluded because its text is a
    placeholder (``Prefix Unveil``) with no mod behind it."""
    subject = item(
        implicit=["+20 to maximum Life"],
        explicit=["+95 to maximum Life"],
        crafted=["+30 to maximum Life"],
        fractured_mods=["+12% to Fire Resistance"],
        veiled=["Prefix Unveil"],
        influences=["hunter"],
    )
    translated = item_mods(subject)
    assert translated.explicit == ["+95 to maximum Life"]
    assert translated.crafted == ["+30 to maximum Life"]
    assert translated.fractured == ["+12% to Fire Resistance"]
    assert translated.influences == ["hunter"]
    assert "Prefix Unveil" not in [text for _origin, text in translated.lines()]


def test_the_highlighter_reads_fractured_lines_too(moddb):
    """A fractured top-tier roll is the reason somebody would buy the item at all,
    and it lives in a different array from ``explicit``."""
    subject = item(
        base_type="Siege Helmet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        fractured_mods=["+130 to maximum Life"],
    )
    assert any(name.startswith("tier:") for name in signals(evaluate(subject, moddb=moddb)))


def test_a_bench_craft_is_not_attributed_and_therefore_claims_no_tier(moddb):
    """The artifact carries the *spawnable* pools, not the crafting bench's.

    A crafted ``+130 to maximum Life`` comes back ``unknown``, and that is the
    correct answer rather than a gap to paper over: the bench ladder is counted from
    a different place, so borrowing the explicit pool's T1 for it would be a tier
    from the wrong ruler. It costs a pre-tick and never a wrong number.
    """
    subject = item(
        base_type="Siege Helmet",
        category="armour",
        subcategory="helmet",
        ilvl=86,
        crafted=["+130 to maximum Life"],
    )
    match = report_for(subject, moddb).matches[0]
    assert match.attribution.value == "unknown"
    assert match.tier is None
    assert not any(name.startswith("tier:") for name in signals(evaluate(subject, moddb=moddb)))
