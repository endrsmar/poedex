"""Tier 2 — the **highlighter** (IMPLEMENTATION-PLAN §5b).

The gate no longer decides anything. It answers one question — *is this item worth
asking the market about?* — and it **claims no number**. What it produces is a
proposal the player accepts with a tick or rejects by leaving it unticked, which is
the whole of the Phase 9 pivot: being wrong now costs a tick rather than a price.

## Why the constants are gone rather than tuned

Every threshold this file used to carry has been deleted. Its own docstring asked for
that ("if a future phase wants real tiers, it needs a mod database and these constants
should be deleted rather than tuned"), and `moddb` is that database. Concretely:

* **``MOD_GROUPS``** — fourteen regexes with one number each, no knowledge of base
  type, item class or item level. ``+95 to maximum Life`` is **T4 of 10** on a helmet
  and **T7 of 13** on a body armour, and one number cannot say that. Replaced by
  :meth:`ModDbApi.report`, which knows both.
* **``ILVL86_BASE_CATEGORIES`` / ``ILVL86_EXCLUDED_SUBCATEGORIES``** — a category
  guess about where item level matters, and **wrong twice**: helmets, gloves and
  rings top out at affix level **85**, so a third of the old ilvl-86 flags on
  jewellery and helmets were noise; and flasks were excluded on the grounds that
  "a flask's mods do not scale with ilvl" when the top flask suffixes need item level
  84-85. Replaced by :attr:`BaseInfo.top_affix_level` and
  :meth:`BaseInfo.fully_rolled`, which are per base.
* **``HIGH_VALUE_BASES``** — twenty-six names typed in from memory. GGG ships the
  factual half of that claim as the ``top_tier_base_item_type`` tag on 187 bases, and
  :attr:`BaseInfo.is_top_tier` reads it. Seven of the twenty-six carry the tag; the
  other nineteen survive here as :data:`SOUGHT_AFTER_BASES`, **explicitly labelled an
  opinion**, and they are a *soft* signal so the strict gate stays entirely factual.

:data:`SIX_LINK` and :data:`ILVL86` stay. Six links is a game constant, and 86 is the
level at which nothing further unlocks anywhere — used **only** as the fallback when
the database has nothing to say about a base, so a missing artifact degrades to the
old behaviour instead of to silence.

## The four criteria

IMPLEMENTATION-PLAN §5b names them: *valuable base at high ilvl, high-tier rolls,
six-link, rare influence mods*. All four are here, and the last one means influence
**mods** — the Shaper/Elder/Crusader/Hunter/Redeemer/Warlord pools `moddb` identifies
— not merely an influenced item. Most influenced items carry no influence mod at all,
because the influence was applied and never exploited, and flagging the item for the
tag would put a highlight on nothing.

Three signals beyond the four are retained deliberately, and the deviation is stated
rather than buried: ``fractured`` and ``synthesised`` (both hard: they are facts on
the item that change what it is worth and cost nothing to read) and ``unidentified``
and ``veiled`` (both soft: the mods cannot be read *at all*, which in a bag is
precisely the item you must not vendor by accident).

## Same code, opposite biases — unchanged

* **Bag → generous.** A false negative tells the player to vendor something good.
* **Stash → strict.** Hard signals only; a generous gate at 818-item scale is noise.

What changed is that ``strict`` is now the *factual* subset rather than the
hard-requirement subset, and they happen to be the same set.
"""

from __future__ import annotations

from collections.abc import Sequence

from modules.appraisal.backend.api import (
    GateResult,
    GateSignal,
    Strictness,
)
from modules.moddb.backend.api import (
    Attribution,
    BaseInfo,
    ItemMods,
    ModDbApi,
    ModMatch,
    ModReport,
)
from modules.poeapi.backend.api import Mods, NormalizedItem, Rarity

__all__ = [
    "ILVL86",
    "NEAR_TOP_TIER",
    "NEAR_TOP_TIER_LADDER",
    "SIX_LINK",
    "SOUGHT_AFTER_BASES",
    "describe_report",
    "evaluate",
    "gate_applies",
    "high_tier",
    "item_mods",
    "report_for",
]

SIX_LINK = 6
"""A game constant. Six linked sockets is six linked sockets on every base there is."""

ILVL86 = 86
"""The **fallback** ceiling, used only when `moddb` cannot name a base.

86 is where the last affix level in the game unlocks, so treating it as "fully
rolled" for an unknown base is the most cautious guess available. Every base the
database *does* know is compared against its own :attr:`BaseInfo.top_affix_level`
instead — which is 85 on helmets, gloves and rings, and the reason a third of the old
ilvl-86 flags were noise.
"""

NEAR_TOP_TIER = 2
"""Generous accepts T1 and T2; strict accepts T1 only."""

NEAR_TOP_TIER_LADDER = 4
"""...but only on a ladder with at least this many tiers.

"T2 of 2" is the *worst* roll a two-tier group has, and calling it near-top would
turn the softest signal in the file into one that fires on almost every item.
"""

SOUGHT_AFTER_BASES: frozenset[str] = frozenset(
    {
        # Jewellery. Bought blank for the implicit, not for the tag.
        "Vermillion Ring",
        "Cerulean Ring",
        "Amethyst Ring",
        "Opal Ring",
        "Steel Ring",
        "Marble Amulet",
        "Citrine Amulet",
        "Turquoise Amulet",
        "Onyx Amulet",
        "Stygian Vise",
        "Crystal Belt",
        # Armour whose implicit or base stat carries the price.
        "Bone Helmet",
        "Sacrificial Garb",
        "Fingerless Silk Gloves",
        "Gripped Gloves",
        "Spiked Gloves",
        "Two-Toned Boots",
        # Weapons bought blank to craft on.
        "Imbued Wand",
        "Opal Sceptre",
    }
)
"""**An opinion, and labelled as one.** Nineteen bases people buy blank to craft on.

This is exactly what is left of the old ``HIGH_VALUE_BASES`` after subtracting the
seven that GGG's own ``top_tier_base_item_type`` tag already covers — Hubris Circlet,
Vaal Regalia, Astral Plate, Titanium Spirit Shield, Convoking Wand, Vaal Rapier,
Jewelled Foil, all of which :attr:`BaseInfo.is_top_tier` now answers from data.

What remains is a claim about the *market*, not about the game: a Stygian Vise is
sought after because of an abyssal socket, and no tag in the game files says so. So
it produces a **soft** signal, off entirely at strict, and it is extendable at
runtime through the ``extra_sought_after_bases`` setting rather than by editing code.
Every entry here is something a person has to re-check each league, which is the
definition of an opinion and the reason it is not mixed in with the facts.
"""

# Rarities the gate has anything useful to say about. Currency, cards, fragments and
# gems are tier-1 territory; SPEC §5.2's "bulk tabs never enter tier 2 or 3" is the
# same rule seen from the stash side.
_GATED_RARITIES: frozenset[Rarity] = frozenset({Rarity.RARE, Rarity.MAGIC})

# ...and the categories on which "is this worth asking about" is a real question.
_GATED_CATEGORIES: frozenset[str] = frozenset(
    {"weapon", "armour", "accessory", "jewel", "flask", "tincture"}
)


def gate_applies(item: NormalizedItem) -> bool:
    """Whether tier 2 has any business looking at this item.

    A Chaos Orb does not get a highlight — it has a bulk price, and running the
    highlighter over it would be spending cycles to reach a foregone conclusion.
    """
    return item.rarity in _GATED_RARITIES and item.category in _GATED_CATEGORIES


def item_mods(item: NormalizedItem) -> ItemMods:
    """`poeapi`'s item, in the shape `moddb` asks for.

    The two-line translation `moddb` deliberately does not do itself: it is core with
    ``requires: []``, and naming ``NormalizedItem`` would hand a JSON-reading module
    ``poeapi``'s whole dependency chain. ``veiled`` lines are left out because their
    text is a placeholder (``Prefix Unveil``) with no mod behind it; ``utility``
    (flask) lines are left out because they are not affixes in the prefix/suffix
    economy the report counts.
    """
    mods: Mods = item.mods
    return ItemMods(
        base_type=item.base_type,
        ilvl=item.ilvl,
        rarity=item.rarity.value,
        implicit=list(mods.implicit),
        explicit=list(mods.explicit),
        crafted=list(mods.crafted),
        fractured=list(mods.fractured),
        enchant=list(mods.enchant),
        influences=list(item.influences),
    )


def report_for(item: NormalizedItem, moddb: ModDbApi | None) -> ModReport | None:
    """`moddb`'s reading of the item, or ``None`` when there is no database.

    ``None`` is a first-class outcome. A missing artifact must degrade the *tier*
    claims and nothing else — the six-link is still a six-link — and it must never be
    mistaken for "the database looked and found nothing interesting".
    """
    if moddb is None or not item.identified:
        return None
    try:
        return moddb.report(item_mods(item))
    except Exception:  # pragma: no cover - a broken artifact must not break a bag
        return None


def high_tier(match: ModMatch, *, strictness: Strictness) -> bool:
    """Is this roll high **on this base**?

    Never a threshold. :attr:`ModMatch.top_group` is `moddb`'s own answer to the
    question a gate actually asks — *is the roll in the best tier the group reaches
    here* — and it survives an uncertain tier, because if every surviving candidate
    is T1 then the roll is top-tier whichever of them it was.

    At ``generous`` a T2 also counts, but only on a ladder of at least
    :data:`NEAR_TOP_TIER_LADDER` tiers. At ``strict``, T1 only.
    """
    if not match.attribution.is_confident:
        # AMBIGUOUS and UNKNOWN expose no tier, and inventing one here would put the
        # exact lie `moddb` refuses to tell back on the screen with our name on it.
        return False
    if match.top_group:
        return True
    if strictness is not Strictness.GENEROUS:
        return False
    span = match.tier_range
    tiers = match.tiers
    return (
        span is not None
        and tiers is not None
        and tiers >= NEAR_TOP_TIER_LADDER
        and span[1] <= NEAR_TOP_TIER
    )


def _fully_rolled(item: NormalizedItem, base: BaseInfo | None) -> bool:
    if base is None:
        return item.ilvl >= ILVL86
    return base.fully_rolled(item.ilvl)


def _base_signals(
    item: NormalizedItem, base: BaseInfo | None, sought: frozenset[str]
) -> tuple[list[GateSignal], list[GateSignal]]:
    """The base half of criterion 1, split into what is factual and what is opinion."""
    hard: list[GateSignal] = []
    soft: list[GateSignal] = []
    rolled = _fully_rolled(item, base)
    ceiling = base.top_affix_level if base is not None else ILVL86

    if base is not None and base.is_top_tier and rolled:
        hard.append(
            GateSignal(
                "top_tier_base",
                f"top-tier base, ilvl {item.ilvl}/{ceiling}",
                hard=True,
                label="top-tier base",
            )
        )
    if item.base_type.casefold() in {name.casefold() for name in sought} and rolled:
        soft.append(
            GateSignal(
                "sought_after_base",
                f"sought-after base (opinion), ilvl {item.ilvl}/{ceiling}",
                label="sought-after base",
            )
        )
    return hard, soft


def _influence_signal(report: ModReport | None) -> GateSignal | None:
    """Criterion 4, and the one most easily got wrong.

    ``item.influences`` says a *tag* was applied. This says a mod **from an influence
    pool is actually on the item**, which is a rarer and much more valuable claim.
    """
    if report is None:
        return None
    mods = report.influence_mods
    if not mods:
        return None
    pools = sorted(pool.value for pool in report.influences)
    return GateSignal(
        "influence_mod",
        f"{'/'.join(pools)} mod" + ("s" if len(mods) > 1 else ""),
        hard=True,
        mods=[match.text for match in mods],
        label="influence mod",
    )


def _tier_signals(report: ModReport | None, strictness: Strictness) -> list[GateSignal]:
    """Criterion 2 — high-tier rolls, one signal per mod, with the tier in the words.

    The detail is :meth:`ModMatch.describe`'s own phrasing (``T2 of 8``), so a surface
    never has to reconstruct a tier from a signal name and can never disagree with the
    database about what was claimed.
    """
    if report is None:
        return []
    signals: list[GateSignal] = []
    for index, match in enumerate(report.matches):
        if not match.origin.is_affix or not high_tier(match, strictness=strictness):
            continue
        signals.append(
            GateSignal(
                f"tier:{index}",
                f"{match.describe()} · {match.text}",
                hard=match.top_group,
                mods=[match.text],
                value=match.value,
                label=match.describe(),
            )
        )
    return signals


def _hard_signals(
    item: NormalizedItem,
    base: BaseInfo | None,
    report: ModReport | None,
    sought: frozenset[str],
) -> tuple[list[GateSignal], list[GateSignal]]:
    """Everything factual, split into (hard, soft-from-the-base-opinion)."""
    hard, soft = _base_signals(item, base, sought)

    if item.sockets.links >= SIX_LINK:
        hard.append(GateSignal("six_link", f"{item.sockets.links}-link", hard=True))

    influence = _influence_signal(report)
    if influence is not None:
        hard.append(influence)

    # Retained beyond §5b's four, deliberately — see the module docstring.
    if item.fractured or item.mods.fractured:
        hard.append(
            GateSignal("fractured", "fractured", hard=True, mods=list(item.mods.fractured))
        )
    if item.synthesised:
        hard.append(GateSignal("synthesised", "synthesised", hard=True))
    return hard, soft


def _soft_signals(item: NormalizedItem, report: ModReport | None) -> list[GateSignal]:
    """The generous gate's additions: the two states where the mods cannot be read."""
    signals: list[GateSignal] = []
    if not item.identified:
        # Unidentified means the mods cannot be read at all, so nothing tier-based can
        # fire. In a bag that is exactly the item you must not vendor by accident.
        return [GateSignal("unidentified", "unidentified")]
    if item.mods.veiled:
        signals.append(GateSignal("veiled", f"{len(item.mods.veiled)} veiled"))
    return signals


def describe_report(report: ModReport | None) -> str:
    """One line about how much of the item the database could actually read.

    Printed next to a highlight so "nothing high-tier here" can be told apart from
    "three of six lines came back unknown, so ask someone else". A gate that cannot
    say which of those it means is a gate whose silence means nothing.
    """
    if report is None:
        return "no mod database — tiers unavailable"
    affixes = [m for m in report.matches if m.origin.is_affix]
    if not affixes:
        return "no affix lines to read"
    confident = sum(1 for m in affixes if m.attribution.is_confident)
    unknown = sum(1 for m in affixes if m.attribution is Attribution.UNKNOWN)
    parts = [f"{confident}/{len(affixes)} lines attributed"]
    if unknown:
        parts.append(f"{unknown} not in the database")
    if not report.counts_are_certain:
        parts.append("open-affix counts uncertain")
    return ", ".join(parts)


def evaluate(
    item: NormalizedItem,
    *,
    strictness: Strictness = Strictness.GENEROUS,
    moddb: ModDbApi | None = None,
    sought_bases: Sequence[str] | frozenset[str] = SOUGHT_AFTER_BASES,
    report: ModReport | None = None,
) -> GateResult:
    """Run the highlighter over one item.

    One function for both strictness levels, so "strict is generous minus the soft
    signals" stays a property of the code rather than a claim about two parallel
    implementations. ``report`` may be passed in when the caller has already asked
    `moddb` — the checkbox list needs the same report — so an item is never read
    twice.
    """
    if not gate_applies(item):
        return GateResult((), strictness=strictness, considered=False)

    names = sought_bases if isinstance(sought_bases, frozenset) else frozenset(sought_bases)
    reading = report if report is not None else report_for(item, moddb)
    base = reading.base if reading is not None else (moddb.base(item.base_type) if moddb else None)

    hard, base_soft = _hard_signals(item, base, reading, names)
    signals = list(hard)
    signals.extend(_tier_signals(reading, Strictness.STRICT))
    if strictness is Strictness.GENEROUS:
        signals.extend(base_soft)
        signals.extend(_soft_signals(item, reading))
        seen = {signal.name for signal in signals}
        signals.extend(
            signal
            for signal in _tier_signals(reading, Strictness.GENEROUS)
            if signal.name not in seen
        )
    return GateResult(signals, strictness=strictness, considered=True)
