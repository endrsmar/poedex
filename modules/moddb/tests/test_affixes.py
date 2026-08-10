"""Prefix/suffix classification and open-affix counting, on real rares.

Open affix counts drive a filter the player can include in a trade search, so being
wrong here is not cosmetic: "1 open prefix" on an item with none returns an empty
search and looks like the tool is broken.
"""

from __future__ import annotations

from typing import Any

from modules.moddb.backend.api import Affix, Attribution, ItemMods, Origin


def a_real_belt() -> ItemMods:
    """Verbatim from ``tests/fixtures/poeapi/get-items.json`` — a live, scrubbed item."""
    return ItemMods(
        base_type="Cloth Belt",
        ilvl=69,
        rarity="rare",
        implicit=["24% increased Stun and Block Recovery"],
        explicit=["+59 to Armour", "+144 to maximum Life", "+41% to Fire Resistance"],
        crafted=["+13% to Lightning and Chaos Resistances"],
    )


def a_real_helmet() -> ItemMods:
    """From ``get-stash-items.json``: two explicits and a bench craft."""
    return ItemMods(
        base_type="Eternal Burgonet",
        ilvl=86,
        rarity="rare",
        explicit=["+95 to maximum Life", "+40% to Cold Resistance"],
        crafted=["+25% to Fire Resistance"],
    )


def test_a_real_rare_is_classified_and_counted(db) -> None:
    report = db.report(a_real_belt())
    assert report.base is not None and report.base.item_class == "Belt"
    assert (report.prefixes, report.suffixes) == (2, 2)
    assert (report.open_prefixes, report.open_suffixes) == (1, 1)
    assert report.unattributed == 0
    assert report.counts_are_certain
    assert report.affixes == 4

    sides = {match.text: match.affix for match in report.matches}
    assert sides["+59 to Armour"] is Affix.PREFIX
    assert sides["+144 to maximum Life"] is Affix.PREFIX
    assert sides["+41% to Fire Resistance"] is Affix.SUFFIX
    assert sides["+13% to Lightning and Chaos Resistances"] is Affix.SUFFIX


def test_the_implicit_is_not_counted_as_an_affix(db) -> None:
    """It comes off the base and occupies no slot — so it cannot close one either."""
    report = db.report(a_real_belt())
    implicit = next(m for m in report.matches if m.origin is Origin.IMPLICIT)
    assert implicit.attribution is Attribution.UNKNOWN
    assert report.affixes == 4  # not 5


def test_a_bench_craft_occupies_a_slot(db) -> None:
    """Which is why a bench craft blocks itself once an item has three prefixes."""
    helmet = db.report(a_real_helmet())
    assert (helmet.prefixes, helmet.suffixes) == (1, 2)
    assert (helmet.open_prefixes, helmet.open_suffixes) == (2, 1)

    without = db.report(
        ItemMods(
            base_type="Eternal Burgonet",
            ilvl=86,
            rarity="rare",
            explicit=["+95 to maximum Life", "+40% to Cold Resistance"],
        )
    )
    assert (without.open_prefixes, without.open_suffixes) == (2, 2)


def test_a_full_suffix_block_reads_as_zero_open(db) -> None:
    report = db.report(
        ItemMods(
            base_type="Coral Ring",
            ilvl=81,
            rarity="rare",
            explicit=[
                "+79 to maximum Life",
                "+41% to Fire Resistance",
                "+39% to Cold Resistance",
                "+38% to Lightning Resistance",
            ],
        )
    )
    assert (report.prefixes, report.suffixes) == (1, 3)
    assert (report.open_prefixes, report.open_suffixes) == (2, 0)
    assert report.counts_are_certain


def test_a_hybrid_writes_two_lines_and_takes_one_slot(db) -> None:
    """``+# to maximum Life`` plus ``#% increased maximum Life`` is one prefix.

    Counting lines would report two, and the open-prefix number a player pastes into
    a filter would be one too low.
    """
    report = db.report(
        ItemMods(
            base_type="Legion Plate",
            ilvl=84,
            rarity="rare",
            explicit=["+26 to Armour", "+18 to maximum Life", "+28% to Cold Resistance"],
        )
    )
    assert report.hybrids == 1
    assert (report.prefixes, report.suffixes) == (1, 1)


def test_an_uncertain_merge_is_declared_rather_than_hidden(db) -> None:
    """Both readings fit every number, and the difference is one open prefix.

    So the shorter reading is used — and ``counts_are_certain`` goes false, because a
    number that might be wrong by one is not a number to build a search on.
    """
    ambiguous = db.report(
        ItemMods(
            base_type="Legion Plate",
            ilvl=84,
            rarity="rare",
            explicit=["+26 to Armour", "+18 to maximum Life", "+28% to Cold Resistance"],
        )
    )
    assert ambiguous.uncertain_merges == 1
    assert not ambiguous.counts_are_certain

    # The same two stats at rolls no hybrid can reach: two prefixes, and certain.
    decided = db.report(
        ItemMods(
            base_type="Legion Plate",
            ilvl=84,
            rarity="rare",
            explicit=["+494 to Armour", "+154 to maximum Life", "+28% to Cold Resistance"],
        )
    )
    assert decided.hybrids == 0
    assert (decided.prefixes, decided.suffixes) == (2, 1)
    assert decided.counts_are_certain


def test_a_rare_jewel_has_two_slots_a_side_not_three(db) -> None:
    report = db.report(
        ItemMods(
            base_type="Cobalt Jewel",
            ilvl=84,
            rarity="rare",
            explicit=["7% increased maximum Life", "14% increased Fire Damage"],
        )
    )
    assert (report.max_prefixes, report.max_suffixes) == (2, 2)
    assert (report.open_prefixes, report.open_suffixes) == (0, 2)


def test_a_magic_item_has_one_slot_a_side(db) -> None:
    report = db.report(
        ItemMods(
            base_type="Basalt Flask",
            ilvl=65,
            rarity="magic",
            explicit=["31% increased Charge Recovery"],
        )
    )
    assert (report.max_prefixes, report.max_suffixes) == (1, 1)
    assert (report.prefixes, report.open_suffixes) == (1, 1)


def test_a_base_with_no_affixes_reports_no_counts(db) -> None:
    report = db.report(ItemMods(base_type="Chaos Orb", explicit=["Reforges a rare item"]))
    assert report.base is None
    assert not report.counts_are_certain
    assert report.to_json()["base"] is None


def test_the_report_serializes(db) -> None:
    payload = db.report(a_real_belt()).to_json()
    assert payload["prefixes"] == 2
    assert payload["open_suffixes"] == 1
    assert payload["counts_are_certain"] is True
    assert len(payload["matches"]) == 5
    assert payload["base"]["item_class"] == "Belt"


def test_every_live_fixture_item_survives_a_report(db, live_items: list[dict[str, Any]]) -> None:
    """No crash, no exception, and a base is resolved for everything that has one.

    The regression this catches is the boring one: a mod line shaped in a way nothing
    anticipated taking the whole report down. A rare that cannot be classified should
    come back as ``unknown``, never as a traceback in a panel.
    """
    seen = 0
    for raw in live_items:
        item = ItemMods(
            base_type=raw.get("baseType") or raw.get("typeLine") or "",
            ilvl=raw.get("ilvl") or 0,
            explicit=raw.get("explicitMods") or (),
            implicit=raw.get("implicitMods") or (),
            crafted=raw.get("craftedMods") or (),
            fractured=raw.get("fracturedMods") or (),
        )
        report = db.report(item)
        assert len(report.matches) == len(item.lines())
        if report.base is not None:
            seen += 1
            assert report.prefixes + report.suffixes + report.unattributed == report.affixes
            assert report.open_prefixes >= 0 and report.open_suffixes >= 0
    assert seen >= 8, "the live fixtures should contain at least eight real gear items"
