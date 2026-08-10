"""Phase 9b — `moddb` against GGG's own answers, on twenty real rares.

Every other test in this project checks `moddb` against `moddb`: the artifact is
built from RePoE and then asserted against expectations written by the same person
who read RePoE. That catches a regression and cannot catch a *shared* mistake, which
is what Phase 9's trade-id bridge turned out to be — it resolved 94.3% of mod lines
to an id and 87.1% to the **right** id, and no offline test could tell the difference
because both halves agreed.

``tests/fixtures/moddb/live_trade_rares.json`` is the outside opinion. Public trade
listings carry, per mod line, the stat id GGG's own filter list uses and GGG's own
tier label, so these tests compare against the authority. Nothing here touches the
network — the fixture is frozen and scrubbed; see its README.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from modules.appraisal.backend.gate import evaluate, report_for
from modules.appraisal.backend.highlight import MAX_PRETICKED, significance
from modules.appraisal.backend.highlight import build as build_highlight
from modules.moddb.backend.api import Origin
from modules.moddb.backend.database import SUMMED_NOTE
from modules.moddb.backend.module import ModDbModule
from modules.poeapi.backend.models import Location, Source
from modules.poeapi.backend.normalize import normalize_item

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "moddb" / "live_trade_rares.json"

_MOD_KEYS = {
    "implicit": "implicitMods",
    "explicit": "explicitMods",
    "crafted": "craftedMods",
    "fractured": "fracturedMods",
    "enchant": "enchantMods",
    "veiled": "veiledMods",
}


@pytest.fixture(scope="module")
def db() -> ModDbModule:
    return ModDbModule()


@pytest.fixture(scope="module")
def rares() -> list[dict[str, Any]]:
    return json.loads(FIXTURE.read_text("utf-8"))["items"]


def _item(record: dict[str, Any], uid: str):
    raw: dict[str, Any] = {
        "id": uid,
        "name": "",
        "typeLine": record["base_type"],
        "baseType": record["base_type"],
        "frameType": 2,
        "ilvl": record["ilvl"],
        "identified": True,
        "influences": {name: True for name in record["influences"]},
    }
    for origin, texts in record["mods"].items():
        raw[_MOD_KEYS[origin]] = list(texts)
    return normalize_item(raw, Location(source=Source.BAG))


def _highlight(record: dict[str, Any], db: ModDbModule, uid: str):
    item = _item(record, uid)
    report = report_for(item, db)
    return build_highlight(item, evaluate(item, moddb=db, report=report), report, moddb=db)


# -- the bridge, against GGG's own filter list -----------------------------------


def test_every_line_resolves_to_the_id_ggg_itself_uses(db, rares) -> None:
    """The check Phase 9 could not make, and the reason Phase 9b exists.

    104 mod lines on 20 real rares, and the id `moddb` hands to a trade filter has to
    be character-for-character the id GGG's own listing carries for that line. Before
    the local/global split was understood this failed on every local defence and
    weapon mod — silently, because a wrong id is still an id and the query it builds
    simply comes back empty.
    """
    checked = 0
    wrong: list[tuple[str, str, str, str | None]] = []
    for index, record in enumerate(rares):
        highlight = _highlight(record, db, f"live-{index}")
        truth = {entry["text"]: entry for entry in record["ggg"]}
        for option in highlight.mods:
            expected = truth.get(option.text)
            if expected is None:
                continue
            ours = db.trade_stat_id(
                option.text, origin=Origin(option.origin), local=option.local
            )
            checked += 1
            if ours != expected["trade_stat_id"]:
                wrong.append((record["base_type"], option.text, expected["trade_stat_id"], ours))
    assert checked >= 100, "the fixture stopped covering what it was recorded for"
    assert wrong == []


def test_the_local_reading_is_what_makes_it_agree(db, rares) -> None:
    """Falsifiable in the other direction: ignoring locality really does break it.

    Without the hint the global id wins wherever a global id exists, which is what
    Phase 9 shipped. If that produced the same answers, the whole Phase 9b change
    would be ceremony — so this asserts that it does not.
    """
    disagreements = 0
    for index, record in enumerate(rares):
        highlight = _highlight(record, db, f"live-{index}")
        truth = {entry["text"]: entry for entry in record["ggg"]}
        for option in highlight.mods:
            expected = truth.get(option.text)
            if expected is None or not option.local:
                continue
            blind = db.trade_stat_id(option.text, origin=Origin(option.origin))
            if blind != expected["trade_stat_id"]:
                disagreements += 1
    assert disagreements > 0, (
        "no local mod in the fixture changes answer without the hint — either the "
        "fixture no longer contains one, or the hint is not being used"
    )


# -- prefix/suffix, against GGG's own labels --------------------------------------


def test_prefix_and_suffix_agree_with_ggg_wherever_moddb_commits(db, rares) -> None:
    """The open-affix filter is built on this, and a wrong count returns nothing.

    GGG labels a dropped affix ``P<n>`` or ``S<n>``; a bench craft gets ``R<n>``,
    which says nothing about the side and is skipped. Where `moddb` names a side it
    has to be the right one — where it declines, it is allowed to decline, and
    :attr:`ModReport.counts_are_certain` is what tells the panel so.
    """
    agreed = withheld = 0
    wrong: list[tuple[str, str, str, str]] = []
    for index, record in enumerate(rares):
        highlight = _highlight(record, db, f"live-{index}")
        truth = {entry["text"]: entry for entry in record["ggg"]}
        for option in highlight.mods:
            expected = truth.get(option.text)
            tier = (expected or {}).get("tier") or ""
            if not tier or tier[0] not in "PS" or (expected or {}).get("affixes", 1) != 1:
                continue
            side = "prefix" if tier.startswith("P") else "suffix"
            if option.affix is None:
                withheld += 1
            elif option.affix == side:
                agreed += 1
            else:
                wrong.append((record["base_type"], option.text, option.affix, tier))
    assert wrong == []
    assert agreed >= 85
    # Withholding is the honest state and must stay rare enough to be worth having.
    assert withheld <= agreed // 10


def test_a_line_the_game_summed_from_two_affixes_is_the_known_trap(db, rares) -> None:
    """The fixture still carries the case. Kept as the guard on the one below."""
    summed = [
        entry
        for record in rares
        for entry in record["ggg"]
        if entry.get("affixes", 1) > 1
    ]
    assert summed, "the fixture no longer carries a summed line; the trap is untested"


# -- defect 3: `moddb` was confident about a line the game added up ----------------


def test_a_summed_line_is_answered_honestly_instead_of_t1_of_8(db, rares) -> None:
    """The named trap, now refused rather than asserted.

    Path of Exile adds two affixes granting the same stat into **one** displayed line.
    ``+161 to Evasion Rating`` on the fixture's Grizzly Pelt is GGG's own ``P2`` with
    ``affixes: 2`` — a hybrid plus a prefix — and `moddb` used to find the single tier
    whose range contains 161, answer ``T1 of 8``, and pre-tick it as a top-tier roll.
    The miss was never the problem: two readings fit the number and nothing on the item
    can separate them. **The confidence was the problem**, and there has been an honest
    state for exactly this since Phase 8.

    So the assertion is about the refusal, not about a better guess: no tier, no
    ceiling, no pre-tick, and a note that names why.
    """
    index, record = next(
        (i, r)
        for i, r in enumerate(rares)
        if any(entry.get("affixes", 1) > 1 for entry in r["ggg"])
    )
    truth = {entry["text"]: entry for entry in record["ggg"] if entry.get("affixes", 1) > 1}
    highlight = _highlight(record, db, f"live-{index}")
    rows = [option for option in highlight.mods if option.text in truth]
    assert rows, "the summed line stopped appearing as a row"
    for row in rows:
        assert row.attribution == "ambiguous", row.text
        assert row.tier is None and row.tiers is None
        assert row.tier_label == "unknown"
        assert row.ceiling is None
        assert not row.preticked


def test_the_refusal_is_narrow_enough_to_be_worth_having(db, rares) -> None:
    """A database that answers "possibly summed" to everything has stopped reading.

    Asked per line, a two-affix reading is *conceivable* almost everywhere a hybrid
    exists — 12 of 99 lines here. Asked against the affix slots the rest of the item
    has already spent, it is usually impossible: a Titanium Spirit Shield with three
    prefixes accounted for cannot be hiding a fourth inside a displayed total. That
    budget check is what takes it to 5, and this pins the ratio so a future widening
    of the pair rule has to argue with a number.
    """
    withheld = checked = 0
    for index, record in enumerate(rares):
        report = report_for(_item(record, f"live-{index}"), db)
        assert report is not None
        truth = {entry["text"] for entry in record["ggg"]}
        for match in report.matches:
            if not match.origin.is_affix or match.text not in truth:
                continue
            checked += 1
            withheld += match.note == SUMMED_NOTE
    assert checked >= 95
    assert withheld, "nothing is refused any more; the summed reading stopped firing"
    assert withheld <= checked // 10, (
        f"{withheld} of {checked} lines refuse a tier for a summed reading; that is "
        "no longer a refusal, it is a policy of not answering"
    )


# -- defect 1: the pre-tick was worst exactly where the item was best --------------


def test_the_six_t1_gloves_no_longer_propose_a_six_filter_conjunction(db, rares) -> None:
    """The measured failure, and the item it was measured on.

    2-divine Soldier Gloves, six T1/T2 rolls, and Phase 9b's pre-tick ticked **6 of
    6** — the exact six-filter conjunction Phase 9 had already measured returning zero
    listings against the live API, sent by the default button press with no broadening
    behind it. Worst on the best item in the sample.
    """
    index, record = next(
        (i, r) for i, r in enumerate(rares) if r["base_type"] == "Soldier Gloves"
    )
    highlight = _highlight(record, db, f"live-{index}")
    assert len(highlight.mods) == 6, "the fixture's six-roll gloves changed shape"
    assert len(highlight.preticked) <= MAX_PRETICKED < 6
    # The rows are all still there and all still tickable. The cap is on the
    # *proposal*; a player who wants six filters ticks six.
    assert all(option.tradeable for option in highlight.mods)
    # ...and the panel says so before the request rather than after the answer.
    assert "left unticked" in highlight.note


def test_the_pre_tick_ranks_on_the_item_level_the_game_demands(db, rares) -> None:
    """What survives the cap, and why — no mod names anywhere in the reason.

    On the gloves the two that survive are the ones the game gated highest: 96%
    increased Armour and Energy Shield needs ilvl 84 and 16% increased Attack Speed
    needs 76. The ``+111 to maximum Life`` that loses is a T2 whose tier unlocks at 44,
    and the added physical damage is a T1 of a four-tier ladder that unlocks at 28.
    That ordering is `moddb`'s own ``required_level`` and nothing else — and it is
    also, on this item, the pair that was measured live to return listings where the
    six returned none.
    """
    index, record = next(
        (i, r) for i, r in enumerate(rares) if r["base_type"] == "Soldier Gloves"
    )
    highlight = _highlight(record, db, f"live-{index}")
    ticked = {option.text for option in highlight.mods if option.preticked}
    assert "96% increased Armour and Energy Shield" in ticked
    assert "16% increased Attack Speed" in ticked
    assert "Adds 4 to 9 Physical Damage to Attacks" not in ticked

    report = report_for(_item(record, f"live-{index}"), db)
    assert report is not None
    by_text = {match.text: match for match in report.matches}
    assert significance(by_text["96% increased Armour and Energy Shield"]) > significance(
        by_text["+111 to maximum Life"]
    )


def test_no_item_in_the_sample_proposes_more_filters_than_the_cap(db, rares) -> None:
    """The distribution, before and after, on the sample it was measured on.

    Phase 9b measured four items at 1 tick, five at 2, five at 3, three at 4, two at
    5 and one at 6 — 57 ticks, median 3, and the six on the item worth the most. The
    cap is 2 because 2 is the number that was measured to *find* something: run live
    against the fixture's Soldier Gloves, six filters and three filters both returned
    zero listings and two returned three. Nothing above the cap survives, and no item
    is left with nothing to propose that had something before.
    """
    counts = []
    for index, record in enumerate(rares):
        highlight = _highlight(record, db, f"live-{index}")
        counts.append(len(highlight.preticked))
        assert len(highlight.preticked) <= len(highlight.mods)
    assert max(counts) <= MAX_PRETICKED
    assert sorted(counts)[len(counts) // 2] == MAX_PRETICKED
    assert sum(counts) < 57, "the cap is not reducing what the default press sends"
