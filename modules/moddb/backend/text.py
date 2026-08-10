"""One normalizer, used by both halves of the bridge.

The build step (``scripts/build_moddb.py``) reduces a database mod's rendered text
— ``"+(85-99) to maximum Life"`` — to a key. The runtime reduces an item's rolled
text — ``"+95 to maximum Life"`` — to the same key. If those two ever stopped being
the same function, every lookup would miss and the module would report "unknown" for
the entire game, which is a failure mode that looks like caution.

It also has to agree with **GGG's** spelling, because ``stat_translations.json``
publishes the trade API's sentences (``"+# to maximum Life"``) and that is the only
place the two id spaces meet.
"""

from __future__ import annotations

import re

__all__ = ["denominate", "normalize_mod_text", "slots_in"]

_RANGE = re.compile(r"\((-?\d+(?:\.\d+)?)-(-?\d+(?:\.\d+)?)\)")
"""``(85-99)`` — how the database spells a roll range, including ``(-25--10)``."""

_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
"""A rolled value. A leading ``+`` is **not** part of it.

``+#`` and ``#`` are different stats to the trade API — ``+# to maximum Life`` is a
flat roll and ``#% increased maximum Life`` is not — so the sign has to survive
normalization. A leading ``-`` is the opposite case: it belongs to the value, and
``(-30--20)%`` and ``-25%`` have to collapse to the same key."""

PLACEHOLDER = "#"


def denominate(text: str) -> tuple[str, tuple[tuple[float, float], ...]]:
    """``text`` with every value replaced by ``#``, plus the values it carried.

    A value is a ``(low, high)`` pair: a database range gives both ends, a rolled
    item gives the same number twice. Order matters — ``Adds # to # Physical
    Damage`` has two slots and the caller compares them positionally.
    """
    staged: list[str] = []
    ranges: list[tuple[float, float]] = []
    cursor = 0
    for match in _RANGE.finditer(text):
        staged.append(text[cursor : match.start()])
        staged.append("\x00")
        ranges.append((float(match.group(1)), float(match.group(2))))
        cursor = match.end()
    staged.append(text[cursor:])
    collapsed = "".join(staged)

    out: list[str] = []
    values: list[tuple[float, float]] = []
    pending = iter(ranges)
    cursor = 0
    for token in re.finditer(f"\x00|{_NUMBER.pattern}", collapsed):
        out.append(collapsed[cursor : token.start()])
        out.append(PLACEHOLDER)
        cursor = token.end()
        if token.group(0) == "\x00":
            values.append(next(pending))
        else:
            number = float(token.group(0))
            values.append((number, number))
    out.append(collapsed[cursor:])
    return "".join(out).strip(), tuple(values)


def normalize_mod_text(text: str) -> str:
    """Just the key. ``"+95 to maximum Life"`` → ``"+# to maximum Life"``."""
    return denominate(text)[0]


def slots_in(normalized: str) -> int:
    """How many values a normalized text carries. The arity is in the string."""
    return normalized.count(PLACEHOLDER)
