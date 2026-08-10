"""Bridging the two id spaces, tested against real mod text.

`prices` resolves mod text through ``/api/trade/data/stats`` at runtime and gets an
opaque ``explicit.stat_3299347043``. RePoE names the same stat ``base_maximum_life``.
``stat_translations.json`` is the only document that carries both, and the build step
walks it — so this module can answer the same question offline, and the two answers
can be checked against each other instead of merely coexisting.

Everything here uses mod lines from ``tests/fixtures/poeapi/``, which came off a live
account, and the recorded ``/api/trade/data/stats`` body in ``tests/fixtures/prices/``.
"""

from __future__ import annotations

from typing import Any

import pytest

from modules.moddb.backend.api import Origin

# Note what is *not* imported here: `prices`. `moddb` is core, and a core module —
# tests included — may not reach a feature module; the boundary tests enforce it.
# The comparison below is therefore against the recorded document both modules read,
# which is the thing they actually have to agree about.

# Verbatim mod lines from get-items.json and get-stash-items.json.
LIVE_LINES = [
    "+59 to Armour",
    "+144 to maximum Life",
    "+41% to Fire Resistance",
    "+494 to Armour",
    "+154 to maximum Life",
    "+28% to Cold Resistance",
    "+40% to Fire Resistance",
    "20% increased Rarity of Items found",
    "145% increased Physical Damage",
    "+280 to Accuracy Rating",
    "+95 to maximum Life",
    "+40% to Cold Resistance",
    "9% increased maximum Life",
    "12% increased Fire Damage",
]


@pytest.mark.parametrize("text", LIVE_LINES)
def test_every_live_mod_line_reaches_a_trade_stat_id(db, text: str) -> None:
    """The bridge has to work on the text the API actually sends, not on ideal text."""
    stat_id = db.trade_stat_id(text)
    assert stat_id is not None, text
    assert stat_id.startswith("explicit.stat_")


@pytest.mark.parametrize("text", LIVE_LINES)
def test_every_live_mod_line_reaches_a_game_stat_id(db, text: str) -> None:
    """The other end. Kept so the mapping is falsifiable in both directions."""
    assert db.game_stat_ids(text), text


def test_the_bridge_agrees_with_the_recorded_trade_document(db, trade_stats: Any) -> None:
    """The two id spaces meet here, so the two implementations must agree.

    ``prices`` builds its index from GGG's live document; this module builds its own
    from repoe-fork's copy of the same mapping. Where the recorded document has an
    entry, the ids must be identical — otherwise a tier lookup and a trade filter are
    quietly talking about different mods.
    """
    compared = 0
    for group in trade_stats["result"]:
        origin = str(group["id"])
        if origin not in {o.value for o in Origin}:
            continue
        for entry in group["entries"]:
            mine = db.trade_stat_id(entry["text"], origin=Origin(origin))
            if mine is None:
                continue
            compared += 1
            assert mine == entry["id"], entry["text"]
    assert compared >= 8, "the recorded document should overlap this database"


def test_the_namespace_follows_the_origin(db) -> None:
    """One sentence, five namespaces. Asking with the wrong one is a wrong filter."""
    text = "+95 to maximum Life"
    assert db.trade_stat_id(text, origin=Origin.EXPLICIT) == "explicit.stat_3299347043"
    assert db.trade_stat_id(text, origin=Origin.IMPLICIT) == "implicit.stat_3299347043"
    assert db.trade_stat_id(text, origin=Origin.CRAFTED) == "crafted.stat_3299347043"
    assert db.trade_stat_id(text, origin=Origin.FRACTURED) == "fractured.stat_3299347043"


def test_a_namespace_a_stat_does_not_have_returns_none(db) -> None:
    """Rather than a plausible id nothing will ever match.

    ``+59 to Armour`` has no implicit namespace and no enchant one. Synthesising
    ``implicit.stat_809229260`` would produce a trade query that finds nothing and
    looks like a dead market rather than a bad filter.
    """
    assert db.trade_stat_id("+59 to Armour", origin=Origin.EXPLICIT) == "explicit.stat_809229260"
    assert db.trade_stat_id("+59 to Armour", origin=Origin.IMPLICIT) is None
    assert db.trade_stat_id("+59 to Armour", origin=Origin.ENCHANT) is None
    assert db.trade_stat_id("+280 to Accuracy Rating", origin=Origin.ENCHANT) is None


def test_a_sentence_the_database_does_not_know_bridges_to_nothing(db) -> None:
    assert db.trade_stat_id("+3 to Wobbliness") is None
    assert db.game_stat_ids("+3 to Wobbliness") == ()


def test_the_bridge_takes_a_rolled_line_not_a_normalized_one(db) -> None:
    """A caller holds ``+95 to maximum Life``; it should not have to know about ``#``."""
    assert db.trade_stat_id("+95 to maximum Life") == db.trade_stat_id("+3 to maximum Life")
    assert db.game_stat_ids("Adds 12 to 30 Physical Damage") == (
        "global_minimum_added_physical_damage",
        "global_maximum_added_physical_damage",
    )


def test_pseudo_stats_are_not_carried(db) -> None:
    """A pseudo stat is an aggregate no single mod produces.

    ``prices`` refuses them for the same reason. Carrying them here would let a
    caller ask for the tier of "+#% total Elemental Resistance", which is not a mod
    and has no tier.
    """
    for stat_ids in db._trade.values():
        for value in stat_ids[1]:
            assert not value.startswith("pseudo")
