"""Tier 0 — note parsing (SPEC §5.0).

The important assertions here are the negative ones. A bag full of chatty notes must
price at *nothing known*, never at zero: `parse_note` returning ``None`` is what makes
the difference between "unpriceable" and "worthless" downstream.
"""

from __future__ import annotations

import pytest

from modules.prices.backend.notes import parse_note


@pytest.mark.parametrize(
    ("note", "amount", "currency", "kind"),
    [
        ("~price 2 divine", 2.0, "divine", "price"),
        ("~b/o 25 chaos", 25.0, "chaos", "b/o"),
        ("~gb/o 3 exalted", 3.0, "exalted", "gb/o"),
        # The two forms research-notes §7 actually observed on the live account.
        ("~price 3 divine", 3.0, "divine", "price"),
        ("~price 2 awakened-sextant", 2.0, "awakened-sextant", "price"),
        # Fractions are how a sub-chaos item is listed.
        ("~price 1/4 chaos", 0.25, "chaos", "price"),
        ("~b/o 3/2 divine", 1.5, "divine", "b/o"),
        # Decimals, casing, padding, and a trailing comment the indexer ignores.
        ("~b/o 0.5 divine", 0.5, "divine", "b/o"),
        ("~B/O 10 CHAOS", 10.0, "chaos", "b/o"),
        ("  ~price   7   chaos  ", 7.0, "chaos", "price"),
        ("~price 5 chaos negotiable", 5.0, "chaos", "price"),
        ("~price 1 mirror-shard", 1.0, "mirror-shard", "price"),
    ],
)
def test_parses_the_forms_players_actually_write(note, amount, currency, kind):
    parsed = parse_note(note)
    assert parsed is not None
    assert parsed.amount == pytest.approx(amount)
    assert parsed.currency == currency
    assert parsed.kind == kind
    assert parsed.text == note.strip()


@pytest.mark.parametrize(
    "note",
    [
        None,
        "",
        "   ",
        "for sale",
        "~b/o",
        "~b/o chaos",
        "~b/o make me an offer",
        "~price divine 3",  # transposed
        "price 3 divine",  # no tilde
        "~offer 3 divine",  # not one of the three prefixes
        "~b/o 0 chaos",  # zero is not a price
        "~b/o -5 chaos",  # the minus makes the number unparseable, not negative
        "~price 1/0 chaos",  # division by zero
        "~price 3",  # no currency
        "~price three divine",
    ],
)
def test_malformed_notes_are_none_not_zero(note):
    assert parse_note(note) is None


def test_the_currency_is_kept_as_a_trade_id_not_a_display_name():
    """The token is what poe.ninja's exchange lines are keyed by, so it must survive
    parsing unchanged apart from case."""
    parsed = parse_note("~price 2 Divination-Scarab-Of-Pilfering")
    assert parsed is not None
    assert parsed.currency == "divination-scarab-of-pilfering"


def test_str_is_readable():
    assert str(parse_note("~price 1/4 chaos")) == "0.25 chaos"
