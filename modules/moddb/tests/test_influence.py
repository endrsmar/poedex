"""Influence mods — which is a different claim from "the item is influenced".

"Rare influence mods" is one of the four highlight criteria of the new design, and it
means influence *mods*. Most influenced items carry five ordinary mods and one
influence mod, and plenty carry none at all because the influence was applied and
never exploited. Highlighting on the item's influence flag would flag all of them.
"""

from __future__ import annotations

import pytest

from modules.moddb.backend.api import Attribution, Influence, ItemMods

SHAPER_HELMET_MOD = "14% increased Area of Effect"
"""A real Shaper helmet suffix. Chosen because it is unmistakably an influence mod:
nothing on an uninfluenced helmet renders that sentence."""


def test_an_influence_mod_needs_the_items_influence_to_be_findable(db) -> None:
    """The tag lives on the *item*, never on the base — and this is easy to get wrong.

    An influence mod's spawn tag is ``helmet_shaper``. A Hubris Circlet base carries
    ``helmet``. Forget to pass the influence and every influence mod in the database
    is unmatchable, and the highlight simply never fires — a silent, total failure
    that looks like "no influenced items dropped".
    """
    without = db.identify(SHAPER_HELMET_MOD, base_type="Hubris Circlet", ilvl=86)
    assert without.attribution is Attribution.UNKNOWN

    with_it = db.identify(
        SHAPER_HELMET_MOD, base_type="Hubris Circlet", ilvl=86, influences=["shaper"]
    )
    assert with_it.attribution is Attribution.EXACT
    assert with_it.influences == frozenset({Influence.SHAPER})
    assert with_it.is_influence_mod
    assert with_it.tier == 1 and with_it.tiers == 3


def test_the_wrong_influence_does_not_unlock_it(db) -> None:
    match = db.identify(
        SHAPER_HELMET_MOD, base_type="Hubris Circlet", ilvl=86, influences=["hunter"]
    )
    assert match.attribution is Attribution.UNKNOWN


def test_ordinary_mods_on_an_influenced_item_are_not_influence_mods(db) -> None:
    """The distinction the highlight depends on."""
    life = db.identify(
        "+95 to maximum Life", base_type="Hubris Circlet", ilvl=86, influences=["shaper"]
    )
    assert life.attribution is Attribution.EXACT
    assert life.influences == frozenset()
    assert not life.is_influence_mod


def test_a_report_separates_the_influence_mods_from_the_rest(db) -> None:
    report = db.report(
        ItemMods(
            base_type="Hubris Circlet",
            ilvl=86,
            rarity="rare",
            influences=["shaper"],
            explicit=["+95 to maximum Life", SHAPER_HELMET_MOD, "+40% to Cold Resistance"],
        )
    )
    assert [m.text for m in report.influence_mods] == [SHAPER_HELMET_MOD]
    assert report.influences == frozenset({Influence.SHAPER})
    assert len(report.matches) == 3


def test_an_influenced_item_with_no_influence_mod_reports_none(db) -> None:
    """Which is the whole point: the flag is on the item, the mods are the question."""
    report = db.report(
        ItemMods(
            base_type="Hubris Circlet",
            ilvl=86,
            rarity="rare",
            influences=["shaper", "elder"],
            explicit=["+95 to maximum Life", "+40% to Cold Resistance"],
        )
    )
    assert report.influence_mods == ()
    assert report.influences == frozenset()


@pytest.mark.parametrize("pool", [i.value for i in Influence])
def test_every_pool_is_reachable(db, pool: str) -> None:
    """All six, including the four RePoE spells by internal name.

    ``basilisk`` is Hunter, ``eyrie`` is Redeemer and ``adjudicator`` is Warlord. If
    that translation were wrong in one direction the mods would simply never match,
    and nothing would say so.
    """
    base = db._bases["hubris circlet"]
    influenced = db._influenced(base, [pool])
    assert len(influenced.tags) > len(base.tags), pool
    found = [
        mod
        for mod in db._mods
        if mod.influence and db._spawns(mod, influenced)
    ]
    assert found, f"no {pool} mods reachable on a helmet"


def test_influence_is_the_intersection_never_the_union(db) -> None:
    """Two possible mods, one of them not an influence mod, means no claim.

    Saying "Hunter mod" when it might be an ordinary roll would paint a highlight on
    an item that has not earned it, which is exactly the failure the new design
    exists to avoid.
    """
    for match in (
        db.identify("+95 to maximum Life", base_type="Hubris Circlet", ilvl=86),
        db.identify("20% increased Rarity of Items found", base_type="Onyx Amulet", ilvl=86),
    ):
        assert match.influences == frozenset()
