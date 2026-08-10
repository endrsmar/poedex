"""The normalizer, which is the join between the database and a live item.

If :func:`denominate` ever produced different keys for ``"+(85-99) to maximum Life"``
and ``"+95 to maximum Life"``, every lookup in the game would miss and the module
would answer "unknown" for everything — a total failure wearing the costume of
caution. These tests are cheap insurance against that being silent.
"""

from __future__ import annotations

import pytest

from modules.moddb.backend.api import denominate, normalize_mod_text, slots_in


@pytest.mark.parametrize(
    ("database_text", "item_text", "key"),
    [
        ("+(85-99) to maximum Life", "+95 to maximum Life", "+# to maximum Life"),
        ("(15-19)% increased Physical Damage", "17% increased Physical Damage",
         "#% increased Physical Damage"),
        ("Adds (10-15) to (25-30) Physical Damage", "Adds 12 to 30 Physical Damage",
         "Adds # to # Physical Damage"),
        ("Regenerate (5-7) Mana per second", "Regenerate 6 Mana per second",
         "Regenerate # Mana per second"),
        ("Cannot be Frozen", "Cannot be Frozen", "Cannot be Frozen"),
    ],
)
def test_a_database_range_and_a_rolled_value_collapse_to_one_key(
    database_text: str, item_text: str, key: str
) -> None:
    assert normalize_mod_text(database_text) == key
    assert normalize_mod_text(item_text) == key


def test_a_leading_plus_survives_and_a_leading_minus_does_not() -> None:
    """``+#`` and ``#`` are different stats to the trade API; a sign inside a value is not.

    ``+# to maximum Life`` is a flat roll and ``#% increased maximum Life`` is not, so
    eating the ``+`` would merge two unrelated stats. A negative roll is the opposite
    case — ``-25%`` and ``(-30--20)%`` are the same mod at different rolls, and the
    minus belongs to the number.
    """
    assert normalize_mod_text("+95 to maximum Life") == "+# to maximum Life"
    assert normalize_mod_text("-25% to Fire Resistance") == "#% to Fire Resistance"
    assert normalize_mod_text("(-30--20)% to Fire Resistance") == "#% to Fire Resistance"


def test_values_come_back_in_order_and_as_ranges() -> None:
    key, values = denominate("Adds (10-15) to (25-30) Physical Damage")
    assert key == "Adds # to # Physical Damage"
    assert values == ((10.0, 15.0), (25.0, 30.0))

    key, values = denominate("Adds 12 to 30 Physical Damage")
    assert values == ((12.0, 12.0), (30.0, 30.0))


def test_a_reversed_range_is_left_alone_here() -> None:
    """The build step orders the ends; the normalizer only reads them.

    Upstream renders negated stats back to front — ``(44-40)% less Duration`` — and
    the swap is done once, at build time, so the artifact never contains a range a
    roll cannot sit inside. Doing it here as well would hide that from whoever reads
    the artifact.
    """
    _key, values = denominate("(44-40)% less Duration")
    assert values == ((44.0, 40.0),)


def test_arity_is_recoverable_from_the_key_alone() -> None:
    """Which is why the artifact stores no arity beside its value slots."""
    assert slots_in("Adds # to # Physical Damage") == 2
    assert slots_in("+# to maximum Life") == 1
    assert slots_in("Cannot be Frozen") == 0


def test_decimals_and_surrounding_whitespace() -> None:
    assert normalize_mod_text("  Regenerate 0.5% of Life per second  ") == (
        "Regenerate #% of Life per second"
    )
    _key, values = denominate("Regenerate (0.5-1.2)% of Life per second")
    assert values == ((0.5, 1.2),)
