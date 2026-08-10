"""Tier 0 — the player's own asking price (SPEC §5.0).

Items and stash tabs carry a ``note`` the player typed to price them, and it arrives
free with every fetch. The grammar is GGG's, understood by the trade site's indexer:

    ~price 2 divine
    ~b/o 25 chaos
    ~price 1/4 chaos
    ~gb/o 3 exalted

Three things about it are easy to get wrong.

**The currency token is a trade id, not a display name.** ``divine``, not "Divine
Orb"; ``awakened-sextant``, not "Awakened Sextant". That is a stroke of luck rather
than a nuisance: poe.ninja's exchange overviews key their lines by exactly the same
ids, so a note resolves against the same tables the market prices come from and no
name-matching table is needed. :class:`NotePrice` therefore keeps the id and leaves
the conversion to whoever holds the tables.

**A note is free text.** Players write ``~b/o offers``, ``~price 5 div`` (an alias
that is not a trade id), or nothing at all. Everything unparseable must come back as
``None`` rather than as zero, or a bag of chatty notes silently prices itself at
nothing.

**Fractions are real.** ``~price 1/4 chaos`` is how a quarter-chaos item is listed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["NotePrice", "parse_note"]

# `~b/o`, `~price`, `~gb/o` — the three prefixes the trade indexer honours. Anything
# after the currency token is ignored: players append comments and the indexer does
# not mind, so neither do we.
_NOTE = re.compile(
    r"""
    ^\s*~\s*
    (?P<kind>b/o|price|gb/o)
    \s+
    (?P<amount>\d+(?:\.\d+)?|\d+\s*/\s*\d+)
    \s+
    (?P<currency>[A-Za-z][A-Za-z0-9'\-]*)
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class NotePrice:
    """A parsed note. The currency is a **trade id**, not a display name."""

    amount: float
    currency: str
    kind: str
    text: str
    """The original note, verbatim. Shown in a detail pane."""

    def __str__(self) -> str:
        return f"{self.amount:g} {self.currency}"


def parse_note(note: str | None) -> NotePrice | None:
    """Parse a ``~price`` / ``~b/o`` note, or return ``None``.

    ``None`` covers every failure — no note, no ``~`` prefix, a word where a number
    should be, a zero or negative amount. There is deliberately no partial success:
    a caller that got a :class:`NotePrice` may rely on the amount being a usable
    positive number.
    """
    if not note:
        return None
    match = _NOTE.match(note)
    if match is None:
        return None
    raw = match.group("amount").replace(" ", "")
    if "/" in raw:
        numerator, _, denominator = raw.partition("/")
        try:
            amount = float(numerator) / float(denominator)
        except (ValueError, ZeroDivisionError):
            return None
    else:
        amount = float(raw)
    if amount <= 0:
        return None
    return NotePrice(
        amount=amount,
        currency=match.group("currency").casefold(),
        kind=match.group("kind").casefold(),
        text=note.strip(),
    )
