"""Defect 2 — a negative roll needs a direction before it needs an id.

Phase 9b measured the whole family and then declined to ship half a fix, which was the
right call: ``-9 to Total Mana Cost of Skills`` has no trade filter *and* no notion of
which way is good, and resolving only the id turns "this row cannot be searched" into
``min: -7.2``, a search that excludes the -9 item it was built from, matches every
worse one, returns listings, and looks fine.

Both halves are here, and the order is the one the defect asks for. The direction is a
fact `moddb` can read off the mod's own reachable range; the id is a fact GGG's
document carries under the other sign. Nothing here touches the network — the two
opaque ids were checked against ``/api/trade/data/stats`` during this phase and pasted
in, the way the rest of this suite handles live-derived constants.

The reason this was invisible for two phases is worth stating: the artifact and the
runtime **disagreed about the sentence**. ``-(4-9) to Total Mana Cost of Skills`` is
stored under the key ``-# to …`` with a positive range, and a rolled ``-9`` normalized
to ``# to …`` with a negative value. Neither half was wrong on its own, and every mod
in the family came back ``unknown mod``, which reads exactly like caution.
"""

from __future__ import annotations

import pytest

from modules.moddb.backend.api import Origin
from modules.moddb.backend.module import ModDbModule
from modules.moddb.backend.text import negated_slots, readings
from scripts.build_moddb import signed_key

MANA_COST = "-9 to Total Mana Cost of Skills"
AMULET = "Onyx Amulet"


@pytest.fixture(scope="module")
def db() -> ModDbModule:
    return ModDbModule()


# -- the two spellings -------------------------------------------------------------


def test_a_negative_line_offers_both_spellings_and_keeps_its_sign() -> None:
    """The primary reading first, because most sentences have only one."""
    assert readings("+95 to maximum Life") == (("+# to maximum Life", ((95.0, 95.0),)),)
    assert readings(MANA_COST) == (
        ("# to Total Mana Cost of Skills", ((-9.0, -9.0),)),
        ("-# to Total Mana Cost of Skills", ((-9.0, -9.0),)),
    )


def test_the_sentence_says_which_slots_carry_the_minus() -> None:
    assert negated_slots("-# to Total Mana Cost of Skills") == (True,)
    assert negated_slots("+# to maximum Life") == (False,)
    assert negated_slots("Adds # to # Physical Damage") == (False, False)


def test_both_spellings_are_real_and_the_first_hit_is_not_the_answer(db) -> None:
    """Why the reading is chosen by the *answer* and not by mere existence.

    ``IncreaseFlatManaCost`` writes its ``-4`` and ``-5`` tiers as ``# to Total Mana
    Cost of Skills`` and the rest as ``-# to …``. Both keys are in the vocabulary, so a
    resolver that stopped at the first spelling that exists answered "nothing that can
    spawn here produces this text" about a mod the artifact carries nine tiers of.
    """
    small = db.identify(
        "-4 to Total Mana Cost of Skills", base_type=AMULET, ilvl=86, origin=Origin.CRAFTED
    )
    large = db.identify(MANA_COST, base_type=AMULET, ilvl=86, origin=Origin.CRAFTED)
    assert small.attribution.value == "exact"
    assert large.attribution is not None and large.candidates, (
        "the deeper tiers of this mod are spelled the other way and came back unknown"
    )
    assert {candidate.group for candidate in large.candidates} == {"IncreaseFlatManaCost"}


# -- the direction ------------------------------------------------------------------


def test_the_ranges_reach_the_caller_in_the_units_the_item_displays(db) -> None:
    """``(4, 9)`` in the artifact is ``(-9, -4)`` on the tooltip, and on the filter."""
    match = db.identify(MANA_COST, base_type=AMULET, ilvl=86, origin=Origin.CRAFTED)
    assert match.value == -9.0
    for candidate in match.candidates:
        assert candidate.low is not None and candidate.high is not None
        assert candidate.low <= -9.0 <= candidate.high


def test_lower_is_better_where_the_whole_ladder_is_negative(db) -> None:
    reduced = db.identify(MANA_COST, base_type=AMULET, ilvl=86, origin=Origin.CRAFTED)
    life = db.identify("+85 to maximum Life", base_type=AMULET, ilvl=86)
    assert reduced.higher_is_better is False
    assert life.higher_is_better is True


def test_an_unreadable_negative_line_still_answers_from_its_own_sign(db) -> None:
    """The case where the least is known and the answer matters most."""
    unknown = db.identify("-9 to Nonexistent Thing", base_type=AMULET, ilvl=86)
    assert not unknown.candidates
    assert unknown.higher_is_better is False


def test_the_ceiling_of_a_downward_ladder_is_its_most_negative_reach(db) -> None:
    """"Best" is not "largest".

    Taking the maximum on a negative ladder returns the *worst* tier in the game and
    then measures every roll against it, which makes a top roll look like 44% of what
    was available — a ceiling pointing the wrong way is worse than no ceiling.
    """
    match = db.identify(MANA_COST, base_type=AMULET, ilvl=86, origin=Origin.CRAFTED)
    ceiling = db.ceiling(MANA_COST, base_type=AMULET, ilvl=86)
    assert ceiling is None or ceiling <= (match.value or 0.0)


# -- the id ------------------------------------------------------------------------


def test_the_build_step_looks_for_the_sentence_under_the_sign_ggg_spells_it_with() -> None:
    """The offline bridge's half, as a pure function.

    Measured against the live documents during this phase: ``+# to Total Mana Cost of
    Skills`` is ``stat_3736589033`` and ``+# Physical Damage taken from Attack Hits``
    is ``stat_3441651621``, and neither exists under the minus an item writes. Four
    sentences in the current artifact's vocabulary are in this family and all four were
    unbridged — annotated "no trade filter" on the panel for a filter that exists.

    The committed artifact still carries the old answer; it is a build product and is
    regenerated per league, which is when this lands. The **query** does not wait for
    that: ``StatIndex`` makes the same fallback against the live document, which is the
    authority anyway.
    """
    assert signed_key("-# to Total Mana Cost of Skills") == "+# to Total Mana Cost of Skills"
    assert signed_key("Non-Channelling Skills Cost -# Mana") == (
        "Non-Channelling Skills Cost +# Mana"
    )
    # Untouched where there is no sign to move.
    assert signed_key("+# to maximum Life") == "+# to maximum Life"
    assert signed_key("#% increased Attack Speed") == "#% increased Attack Speed"
