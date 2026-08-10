"""The stash, judged — the two opinions a stash needs and a bag does not.

Phase 10 extends the highlighter and the manual price check from the bag to stash
tabs. It **reuses** them: same :meth:`AppraisalApi.highlight`, same
:meth:`AppraisalApi.price_check`, same checkbox list, same ``MAX_PRETICKED``. The
types those answers travel in live in ``api.py`` with the rest of the public surface
(:class:`~modules.appraisal.backend.api.TabSummary`,
:class:`~modules.appraisal.backend.api.StashDigest`,
:class:`~modules.appraisal.backend.api.TabAppraisal`), because a dependent — the CLI
included — may import only that file. What is left here is the one judgement that
needed the gate to make it.

## The gate is strict here, and that is not a tuning knob

SPEC §5.2, unchanged since it was written: the bag wants a **generous** gate because
a false negative tells the player to vendor something good, and the stash wants a
**strict** one because the item is already safe and a generous gate over 818 items
produces hundreds of false positives. The code is the same function with the same
parameter (:func:`~modules.appraisal.backend.gate.evaluate`); nothing here
re-implements it.

Strict drops the soft signals: the sought-after-base *opinion*, the unidentified and
veiled flags, and the near-top-tier (T2-on-a-long-ladder) rolls. What survives is the
factual set — influence mods, six links, fractured, synthesised, a top-tier base at
its own ilvl ceiling, and T1-on-this-base rolls.

## A bulk tab never enters the gate

SPEC §5.2's other half, and :func:`classify` is it. A tab with nothing gateable in it
is ``bulk``; on the measured account that is most of the stash, whose largest tabs are
214 divination cards and 133 fragments. It is a **report** as much as an optimisation —
a digest row that says "214 cards, bulk" has answered the question the reader was
about to ask.
"""

from __future__ import annotations

from collections.abc import Sequence

from modules.appraisal.backend.api import Composition, Strictness
from modules.appraisal.backend.gate import gate_applies
from modules.poeapi.backend.api import NormalizedItem

__all__ = ["STASH_STRICTNESS", "classify"]

STASH_STRICTNESS = Strictness.STRICT
"""What a stash tab is gated at, and it is a *fact about the surface*, not a default.

Kept apart from ``appraisal.strictness``: that setting is the bag's, and a player who
loosens the bag gate has said nothing about whether they want 800 stash items flagged.
The stash has its own setting (``stash_strictness``) and this is its default.
"""


def classify(items: Sequence[NormalizedItem]) -> Composition:
    """Bulk / gear / mixed, from the items themselves.

    The test is :func:`~modules.appraisal.backend.gate.gate_applies` — the *same*
    predicate the gate uses per item — so a tab called ``bulk`` is exactly a tab on
    which the gate would have said nothing. Deriving it from the tab's declared type
    instead would be a guess: a premium tab holds whatever the player put in it, and
    the measured stash has a "premium" tab of 125 fragments.

    There is deliberately no ratio and no threshold. All, none, or some.
    """
    total = len(items)
    if total == 0:
        return Composition.EMPTY
    gateable = sum(1 for item in items if gate_applies(item))
    if gateable == 0:
        return Composition.BULK
    if gateable == total:
        return Composition.GEAR
    return Composition.MIXED
