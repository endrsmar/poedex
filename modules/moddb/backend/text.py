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

__all__ = ["denominate", "negated_slots", "normalize_mod_text", "readings", "slots_in"]

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


def readings(text: str) -> tuple[tuple[str, tuple[tuple[float, float], ...]], ...]:
    """Every way this line's numbers could be split from its words, best guess first.

    A negative value is spelled two ways upstream and the rolled line cannot say
    which. ``(-30--20)%`` keeps the sign **inside** the value, so the key is ``#%``;
    ``-(4-9) to Total Mana Cost of Skills`` keeps it **in the sentence**, so the key
    is ``-# to Total Mana Cost of Skills``. Both are in the artifact's vocabulary, and
    a rolled ``-9`` matches the first spelling and misses the second — which is why
    every reduced-cost mod in the game came back ``unknown mod`` until now: not a
    missing sentence, a normalizer that disagreed with the one the artifact was built
    with, on exactly the family of mods whose sign matters most.

    So both readings are offered and the *vocabulary* decides. The value keeps its
    sign in both — ``-9`` stays ``-9``, never ``9`` — because the number a trade
    filter has to carry is the one the item displays, and the artifact's ranges are
    turned into displayed units when it is loaded (:func:`negated_slots`).
    """
    normalized, values = denominate(text)
    if not any(low < 0 or high < 0 for low, high in values):
        return ((normalized, values),)
    out: list[str] = []
    slot = 0
    for index, char in enumerate(normalized):
        if char == PLACEHOLDER:
            negative = slot < len(values) and min(values[slot]) < 0
            if negative and not (index and normalized[index - 1] == "-"):
                out.append("-")
            slot += 1
        out.append(char)
    alternate = "".join(out)
    if alternate == normalized:
        return ((normalized, values),)
    return ((normalized, values), (alternate, values))


def negated_slots(normalized: str) -> tuple[bool, ...]:
    """Which of a key's value slots the sentence already carries a ``-`` for.

    ``-# to Total Mana Cost of Skills`` → ``(True,)``. The artifact stores that mod's
    range as the positive ``(4, 9)``, because the minus lives in the text; a consumer
    asking "is this a good roll" needs ``(-9, -4)``, which is what the item shows and
    what a trade filter is written in. :class:`ModDatabase` flips them on load using
    this, so nothing downstream has to know the sentence ever held the sign.
    """
    flags: list[bool] = []
    for index, char in enumerate(normalized):
        if char == PLACEHOLDER:
            flags.append(bool(index) and normalized[index - 1] == "-")
    return tuple(flags)


def slots_in(normalized: str) -> int:
    """How many values a normalized text carries. The arity is in the string."""
    return normalized.count(PLACEHOLDER)
