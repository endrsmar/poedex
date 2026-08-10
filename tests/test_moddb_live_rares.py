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
from modules.appraisal.backend.highlight import build as build_highlight
from modules.moddb.backend.api import Origin
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
    """Recorded because it is a real hazard `moddb` does not model.

    Path of Exile adds two affixes granting the same stat into **one** displayed line.
    ``+161 to Evasion Rating`` in this fixture is a P2 hybrid plus a P3 prefix; asked
    about the sum, `moddb` finds the single tier whose range contains 161 and answers
    ``T1 of 8`` — confidently, and about a roll that never happened. It then pre-ticks
    it as a top-tier mod.

    This test does not assert the behaviour is right. It asserts the fixture still
    contains the case, so that whoever fixes it has the example in front of them.
    """
    summed = [
        entry
        for record in rares
        for entry in record["ggg"]
        if entry.get("affixes", 1) > 1
    ]
    assert summed, "the fixture no longer carries a summed line; the trap is untested"
