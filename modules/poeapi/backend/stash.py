"""What a stash tab *is* — kinds, layouts, the tree, and the crawl's bookkeeping.

Facts about Path of Exile, so they live in a core module (IMPLEMENTATION-PLAN §1.2).
Nothing here decides whether a tab is worth looking at; that is `appraisal`'s job.

Four things this file exists to get right, each of them measured rather than assumed
(research-notes §7):

* **Remove-only tabs can never gain items.** 101 of the 117 tabs on the measured
  Standard account are remove-only — 86% — and a tab that cannot change is a tab that
  is fetched once and cached forever. :func:`tab_ttl` is where that rule lives, and it
  is the highest-leverage line in the feature: it takes a full refresh from ~34
  minutes to ~45 seconds.
* **A quad tab is 24x24, not 12x12.** Drawing a quad on a 12x12 lattice silently
  drops three quarters of it.
* **Special tabs have no lattice at all.** Currency, essence, fragment and
  divination-card tabs place items in bespoke slots, and there ``stackSize``
  legitimately exceeds ``maxStackSize`` (``Vaal Orb 163/20`` was measured). Any code
  that assumes one item per grid cell is wrong for them, so :data:`LAYOUTS` gives them
  ``grid=False`` and a surface lists them instead of placing them.
* **Folders exist in PoE and were not observed on this account.** So the tab list is
  a *tree* — :attr:`StashTab.children`, :func:`flatten` — with a flat fast path that
  costs nothing when there are no folders, and no folder UI is built on speculation.

Map tabs are the unresolved one, and :data:`MAP_UNSUPPORTED` carries the honest
sentence. See :func:`unsupported_reason`.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from modules.poeapi.backend.models import StashTab, TabKind, TabLayout

__all__ = [
    "FOREVER",
    "LAYOUTS",
    "MAP_UNSUPPORTED",
    "PER_REQUEST_SECONDS",
    "QUAD_COLUMNS",
    "STANDARD_COLUMNS",
    "crawl_key",
    "estimate_seconds",
    "flatten",
    "layout_for",
    "tab_kind",
    "tab_ttl",
    "tabs_from",
    "unsupported_reason",
]

STANDARD_COLUMNS = 12
"""A normal or premium stash tab is 12x12. (The *backpack* is 12x5; different grid.)"""

QUAD_COLUMNS = 24
"""A quad tab is 24x24 — four normal tabs, not a bigger one."""

FOREVER = math.inf
"""The TTL of a remove-only tab. Not a large number: a genuinely infinite one, so
that no clock drift, no long-running process and no future tuning of "how long is
long enough" can turn "this can never change" into "ask again eventually"."""

MAP_UNSUPPORTED = (
    "map stash tabs are not supported yet: this endpoint returns no items for them, "
    "and GGG's own stash API models a map tab as a parent with child tabs that have "
    "to be fetched one at a time. Reported as unknown rather than as zero, because "
    "an empty answer here is far more likely to mean 'not traversed' than 'empty'."
)
"""Why a map tab says *not supported yet* instead of showing a confident zero.

The evidence, stated so the next person can overturn it rather than re-measure it:

* Five map tabs across two leagues have now been sampled through
  ``/character-window/get-stash-items?tabIndex=N`` — three in research-notes §7 and
  two more in Phase 10, one of them an *active* tab. Every one returned
  ``items: []``.
* GGG's documented stash API (``GET /stash/<league>/<stash_id>[/<substash_id>]``)
  gives :class:`StashTab` a ``children`` array and says the list endpoint "includes
  sub-tabs and stash tabs in folders", while the substash parameter exists so that
  "the inner tab will be wrapped by the parent". A map tab is a parent.
* A player asking GGG how to read a map stash was told it "needs 1 call per map
  type", which is the same shape: one request per child, not one per tab.

That API is OAuth-only, and OAuth is blocked on Cloudflare in Steam's CEF browser
(SPEC §11). So this is a *known* gap with a named fix, not a mystery — and until the
fix exists the honest output is a hole in the total rather than a zero in it.
"""

# GGG's ``type`` strings, minus the ``Stash`` suffix, casefolded. Kept as a table
# rather than a chain of ``if``s so that an unknown type is a *lookup miss* with a
# safe fallback, instead of falling through to whichever branch happens to be last.
_KINDS: Mapping[str, TabKind] = {
    "normal": TabKind.NORMAL,
    "premium": TabKind.PREMIUM,
    "quad": TabKind.QUAD,
    "currency": TabKind.CURRENCY,
    "essence": TabKind.ESSENCE,
    "fragment": TabKind.FRAGMENT,
    "divinationcard": TabKind.DIVINATION,
    "map": TabKind.MAP,
    "delve": TabKind.DELVE,
    "blight": TabKind.BLIGHT,
    "metamorph": TabKind.METAMORPH,
    "delirium": TabKind.DELIRIUM,
    "ultimatum": TabKind.ULTIMATUM,
    "flask": TabKind.FLASK,
    "gem": TabKind.GEM,
    "unique": TabKind.UNIQUE,
    "folder": TabKind.FOLDER,
}

LAYOUTS: Mapping[TabKind, TabLayout] = {
    TabKind.NORMAL: TabLayout(cols=STANDARD_COLUMNS, rows=STANDARD_COLUMNS, grid=True),
    TabKind.PREMIUM: TabLayout(cols=STANDARD_COLUMNS, rows=STANDARD_COLUMNS, grid=True),
    TabKind.QUAD: TabLayout(cols=QUAD_COLUMNS, rows=QUAD_COLUMNS, grid=True),
}
"""Which tabs have a lattice, and how big it is.

Everything absent from this table is drawn as a **list**, not as a grid — and that is
the conservative direction on purpose. A special tab's ``x``/``y`` are bespoke slot
coordinates rather than inventory cells, so placing them on an invented lattice
produces a picture that looks right and is not; listing them shows every item. An
unrecognised tab type lands here too, so a stash type GGG adds next league degrades
to "we can list it" rather than to a mis-drawn grid.
"""


def tab_kind(raw: str) -> TabKind:
    """``"QuadStash"`` → :attr:`TabKind.QUAD`. Unknown types come back as
    :attr:`TabKind.UNKNOWN` rather than being guessed at."""
    token = (raw or "").strip().casefold()
    if token.endswith("stash"):
        token = token[: -len("stash")]
    return _KINDS.get(token, TabKind.UNKNOWN)


def layout_for(kind: TabKind) -> TabLayout:
    return LAYOUTS.get(kind, TabLayout(cols=None, rows=None, grid=False))


def unsupported_reason(kind: TabKind, *, item_count: int | None = None) -> str | None:
    """Why this tab's contents cannot be read, or ``None`` when they can.

    ``item_count`` is what a fetch actually returned. A map tab that comes back with
    items is *not* reported unsupported — if GGG changes the endpoint, or if this
    reading of it is simply wrong, the tool should show the items rather than keep
    insisting they cannot exist. It is the **zero** that is untrustworthy here, and
    only the zero.
    """
    if kind is TabKind.MAP and (item_count is None or item_count == 0):
        return MAP_UNSUPPORTED
    if kind is TabKind.FOLDER:
        return (
            "this is a folder, not a tab: its contents are the tabs inside it, and "
            "each of those is fetched on its own"
        )
    return None


def tab_ttl(tab: StashTab | None, default: float) -> float:
    """How long a fetched copy of this tab stays good for.

    :data:`FOREVER` for a remove-only tab, ``default`` for everything else. This is
    the whole of research-notes §7's headline rule, and it is one line because the
    fact it rests on is one fact: **a remove-only tab cannot gain items**. It can
    only lose them, and every way of losing one goes through the player, who can
    press refresh.
    """
    if tab is not None and tab.remove_only:
        return FOREVER
    return default


def tabs_from(payload: Any) -> list[StashTab]:
    """Build the tab list from a ``tabs=1`` response.

    Returns the **roots** of a tree. On every account measured so far that is a flat
    list, and :func:`flatten` costs one pass over it; a folder, if one ever appears,
    lands as a :attr:`TabKind.FOLDER` root with its members in ``children`` rather
    than as a sibling nobody expected.
    """
    if not isinstance(payload, Mapping):
        return []
    entries = payload.get("tabs")
    if not isinstance(entries, list):
        return []

    built: list[StashTab] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        built.append(_tab_from(entry, index))
    return _nest(built)


def _tab_from(entry: Mapping[str, Any], position: int) -> StashTab:
    from modules.poeapi.backend.normalize import strip_set_tokens

    name = strip_set_tokens(entry.get("n") or entry.get("name"))
    kind = tab_kind(str(entry.get("type") or ""))
    parent = entry.get("parent")
    return StashTab(
        index=_int(entry.get("i"), position),
        id=entry.get("id") if isinstance(entry.get("id"), str) else None,
        parent=parent if isinstance(parent, str) and parent else None,
        name=name,
        type=str(entry.get("type") or ""),
        kind=kind,
        layout=layout_for(kind),
        colour=_colour(entry.get("colour")),
        hidden=bool(entry.get("hidden", False)),
        # GGG spells it in the tab's own name — "(Remove-only) Betrayal". There is no
        # boolean field for it, which is why this is a string test rather than a flag
        # read, and why the check is casefolded rather than exact.
        remove_only="remove-only" in name.casefold(),
        supported=unsupported_reason(kind) is None,
        unsupported_reason=unsupported_reason(kind),
    )


def _nest(tabs: Sequence[StashTab]) -> list[StashTab]:
    """Attach children to their parents by id. A flat list stays a flat list."""
    by_id = {tab.id: tab for tab in tabs if tab.id}
    if not any(tab.parent for tab in tabs):
        return list(tabs)
    children: dict[str, list[StashTab]] = {}
    roots: list[StashTab] = []
    for tab in tabs:
        if tab.parent and tab.parent in by_id:
            children.setdefault(tab.parent, []).append(tab)
        else:
            roots.append(tab)
    return [
        tab.model_copy(update={"children": children.get(tab.id or "", [])}) for tab in roots
    ]


def flatten(tabs: Iterable[StashTab]) -> Iterator[StashTab]:
    """Depth-first over the tree. The fast path for a stash with no folders."""
    for tab in tabs:
        yield tab
        if tab.children:
            yield from flatten(tab.children)


PER_REQUEST_SECONDS = 1.0
"""How long one tab takes when nothing is throttling. SPEC §6.6's "1 request, ~1s"."""


def estimate_seconds(
    requests: int,
    buckets: Sequence[tuple[int, float]],
    fallback: float,
) -> float:
    """How long ``requests`` tab reads will take against the account's real buckets.

    **Not `requests` multiplied by the sustained rate, and the difference is the whole
    usefulness of the number.** GGG's measured item policy is two buckets —
    ``30:60`` and ``100:1800`` (research-notes §3) — so the first thirty requests
    cost about a minute and the hundred-and-first costs half an hour. Quoting the
    sustained figure (one per 18 s) for both makes a **15-tab refresh look like four
    and a half minutes when it is nearer fifteen seconds**, and a warning that
    overstates by an order of magnitude is a warning people learn to press through.

    The model is what a fixed-window limiter actually does: a bucket allows
    ``max_hits`` within ``period`` seconds, so ``n`` requests wait out
    ``floor((n - 1) / max_hits)`` whole windows. The binding bucket wins, and the
    floor is one second per request, because that is what a request costs when
    nothing is stopping it.

    Deliberately ignores how full the buckets *currently* are: a crawl started on a
    half-spent minute finishes a few seconds later than this says, and an estimate
    that changed every time you looked at it would be worse than one that is slightly
    optimistic and stable.
    """
    if requests <= 0:
        return 0.0
    floor_seconds = requests * PER_REQUEST_SECONDS
    usable = [(cap, period) for cap, period in buckets if cap > 0 and period > 0]
    if not usable:
        # Nothing learned yet. The seed budget is pessimistic by design, and so is
        # this: one request per measured interval, which is what the limiter will do.
        return max(floor_seconds, (requests - 1) * fallback)
    windows = max(((requests - 1) // cap) * period for cap, period in usable)
    return max(floor_seconds, windows)


def crawl_key(account: str, league: str) -> str:
    """Storage key for a crawl's progress. Hashed for the same reason cache keys are:
    account and league names are not restricted to what a storage key may contain."""
    digest = hashlib.sha1(f"{account}:{league}".encode()).hexdigest()
    return f"crawl-{digest}.json"


def _colour(value: Any) -> str | None:
    if not isinstance(value, Mapping):
        return None
    red = _int(value.get("r")) & 0xFF
    green = _int(value.get("g")) & 0xFF
    blue = _int(value.get("b")) & 0xFF
    return f"#{red:02x}{green:02x}{blue:02x}"


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)
