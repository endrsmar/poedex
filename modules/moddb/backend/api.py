"""The public surface of the `moddb` core module.

Everything a dependent may import lives here. The database itself is a committed
artifact built by ``scripts/build_moddb.py`` from repoe-fork; nothing at runtime
fetches anything, and there is no code path that could.

## What this module is for

Automatic rare pricing is abandoned — it failed twice against a live account, once
by querying every mod and finding zero listings, once by querying one loose mod and
reporting 10c for a 1c item. No heuristic recovers *which mods make an item
interesting*; that is player knowledge. So the model is Awakened PoE Trade's: the
tool highlights items that are **potentially** expensive, shows their mods as a
checkbox list with the significant ones pre-ticked, and the player decides.

This module supplies the facts that make highlighting and pre-ticking correct rather
than guessed:

* :meth:`ModDbApi.identify` — the tier of a rolled mod, on this base, at this item
  level, with the total tier count and the reachable range.
* :meth:`ModDbApi.report` — prefix/suffix classification and open affix counts.
* :attr:`ModMatch.influences` — whether a mod came from an influence pool, which is
  a different and much rarer claim than "the item is influenced".
* :meth:`ModDbApi.base` — a base's real tags and the highest affix level it can
  reach, replacing hand-typed allowlists.
* :attr:`ModMatch.ceiling` — the best this mod can roll *here*, which is what turns
  "is 85 life good?" into a question with an answer.

## Honesty is a return type, not a docstring

Deciding "this +85 life is T2" means deciding *which mod produced it*, and that is
often genuinely undecidable from the text alone: several groups can render the same
sentence, a hybrid mod produces several sentences, and tier bands from different
pools overlap. Where the answer cannot be pinned down, :class:`ModMatch` says so
through :class:`Attribution` and refuses to expose a tier at all. A surface that
shows "T2" when it means "probably T2, possibly T3" is worse than one that shows
nothing: the first is a lie the player acts on.

:data:`Attribution.EXACT` and :data:`Attribution.GROUP` are the states where a tier
exists. :data:`Attribution.AMBIGUOUS` and :data:`Attribution.UNKNOWN` are the states
where :attr:`ModMatch.tier` is ``None`` and stays ``None``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from modules.moddb.backend.text import denominate, normalize_mod_text, slots_in
from runtime.errors import PoedexError

__all__ = [
    "Affix",
    "Attribution",
    "BaseInfo",
    "DbVersion",
    "Influence",
    "ItemMods",
    "ModCandidate",
    "ModDbApi",
    "ModDbError",
    "ModDbUnavailable",
    "ModMatch",
    "ModReport",
    "Origin",
    "denominate",
    "normalize_mod_text",
    "slots_in",
]


class ModDbError(PoedexError):
    """Base class for this module's errors."""


class ModDbUnavailable(ModDbError):
    """The committed artifact is missing, unreadable, or of an unknown schema.

    Raised loudly rather than degraded to an empty database. An empty mod database
    answers every question with "unknown", which is indistinguishable from a
    correctly cautious one — the failure would look exactly like the feature working.
    """


class Affix(StrEnum):
    PREFIX = "prefix"
    SUFFIX = "suffix"


class Origin(StrEnum):
    """Where a mod line came from on the item, as ``poeapi``'s model already splits it.

    The item endpoints hand these back as separate arrays, so the caller always knows
    which one a line belongs to — and that knowledge removes a whole class of
    ambiguity for free. A ``+# to maximum Life`` in ``craftedMods`` cannot be the
    explicit life prefix, so it is never a candidate.
    """

    EXPLICIT = "explicit"
    IMPLICIT = "implicit"
    CRAFTED = "crafted"
    FRACTURED = "fractured"
    ENCHANT = "enchant"

    @property
    def is_affix(self) -> bool:
        """Whether a line of this origin occupies one of a rare's six affix slots.

        Implicits come from the base and enchants sit outside the prefix/suffix
        economy entirely, so neither is counted. A fractured mod *is* an explicit
        that was frozen, and a crafted mod does occupy a slot — that is why a bench
        craft blocks itself once the item has three prefixes.
        """
        return self in {Origin.EXPLICIT, Origin.FRACTURED, Origin.CRAFTED}


class Influence(StrEnum):
    """The six influence pools.

    RePoE spells four of them by internal name — ``basilisk``, ``eyrie``,
    ``adjudicator``, ``crusader`` — and the build step translates. These are the
    names the trade site, the player and ``NormalizedItem.influences`` all use.
    """

    SHAPER = "shaper"
    ELDER = "elder"
    CRUSADER = "crusader"
    HUNTER = "hunter"
    REDEEMER = "redeemer"
    WARLORD = "warlord"


class Attribution(StrEnum):
    """How sure the database is about *which mod* produced a line of text."""

    EXACT = "exact"
    """One mod, or several that agree on group and tier. A tier may be shown."""

    GROUP = "group"
    """The group is certain, the tier is not — several tiers of it could have rolled
    this value on this base. :attr:`ModMatch.tier_range` is the honest answer and
    :attr:`ModMatch.tier` stays ``None``."""

    AMBIGUOUS = "ambiguous"
    """More than one **ladder** could have produced this sentence here — a different
    group, or the same group as a bench craft, an essence roll or an influence roll.
    Nothing about a tier may be claimed, because the numbers would be counted from
    different places. The candidates are still on the match; only the claim is
    withheld."""

    UNKNOWN = "unknown"
    """No mod in the database produces this text on this base at this item level —
    either the text is not in the database at all (a new league, most likely, and
    the artifact is stale) or nothing that can spawn here rolls it."""

    @property
    def is_confident(self) -> bool:
        return self in {Attribution.EXACT, Attribution.GROUP}


@dataclass(frozen=True, slots=True)
class DbVersion:
    """What this database is, and how old.

    Exposed so a surface can say it out loud. A mod database that is one league
    stale does not fail — it answers confidently and wrongly, and the player has no
    way to tell. ``poedex moddb`` and any panel showing tiers should carry
    :meth:`describe` somewhere the eye lands.
    """

    game_version: str
    generated_at: datetime
    source: str
    mods: int
    bases: int
    texts: int
    schema: int

    def age_days(self, now: datetime | None = None) -> float:
        moment = now or datetime.now(UTC)
        return max(0.0, (moment - self.generated_at).total_seconds() / 86400.0)

    def describe(self, now: datetime | None = None) -> str:
        days = self.age_days(now)
        when = "today" if days < 1 else f"{days:.0f} days old"
        return (
            f"mod database for Path of Exile {self.game_version} "
            f"({self.mods} affixes, {self.bases} bases), built {when}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "game_version": self.game_version,
            "generated_at": self.generated_at.isoformat(),
            "source": self.source,
            "mods": self.mods,
            "bases": self.bases,
            "texts": self.texts,
            "schema": self.schema,
            "age_days": round(self.age_days(), 2),
            "description": self.describe(),
        }


@dataclass(frozen=True, slots=True)
class BaseInfo:
    """What the game knows about a base type.

    This replaces two hardcoded constants in ``modules/appraisal/backend/gate.py``:
    ``ILVL86_BASE_CATEGORIES`` (a category guess about where item level matters) and
    ``HIGH_VALUE_BASES`` (twenty-six names typed in from memory). Both are answered
    here from data — :attr:`top_affix_level` per base, and GGG's own
    ``top_tier_base_item_type`` tag.
    """

    name: str
    item_class: str
    tags: frozenset[str]
    drop_level: int
    top_affix_level: int
    """The item level at which this base can reach every tier it will ever reach.

    The highest ``required_level`` of any affix an ordinary drop of this base can
    roll, with influence pools and essence-only mods excluded since neither is
    reachable without a currency item. 86 on body armours, weapons, belts and
    amulets; **85** on helmets, gloves and rings; 78 on jewels.

    This is the honest replacement for ``gate.py``'s ``ILVL86_BASE_CATEGORIES``, and
    it disagrees with it twice. The gate flags *any* ilvl-86 weapon, armour or
    accessory as special: on a ring or a helmet, 86 buys nothing that 85 did not
    already, so a third of those flags are noise. And it excludes flasks on the
    grounds that "a flask's mods do not scale with item level" — the data says the
    top flask suffixes need item level 84 and 85.
    """

    @property
    def ilvl_matters(self) -> bool:
        """Whether item level gates anything at all on this base.

        Nearly always true; the useful question is the *number*, not the boolean.
        Compare an item's level against :attr:`top_affix_level` with
        :meth:`fully_rolled` rather than against a constant.
        """
        return self.top_affix_level > self.drop_level

    def fully_rolled(self, ilvl: int) -> bool:
        """Whether an item of this level can reach every tier this base has.

        The question ``gate.py``'s ``ilvl >= 86`` was reaching for, asked per base
        instead of per category.
        """
        return ilvl >= self.top_affix_level

    @property
    def is_top_tier(self) -> bool:
        """GGG's own ``top_tier_base_item_type`` marker — the best base of its class.

        Not the same claim as "expensive", and it should not be used as one. It is
        the factual half of what ``HIGH_VALUE_BASES`` was trying to say.
        """
        return "top_tier_base_item_type" in self.tags

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "item_class": self.item_class,
            "tags": sorted(self.tags),
            "drop_level": self.drop_level,
            "top_affix_level": self.top_affix_level,
            "ilvl_matters": self.ilvl_matters,
            "is_top_tier": self.is_top_tier,
        }


@dataclass(frozen=True, slots=True)
class ModCandidate:
    """One mod the database thinks could have produced a line of text."""

    group: str
    affix: Affix
    tier: int
    tiers: int
    required_level: int
    low: float | None
    high: float | None
    """The reachable range of the *first* value slot of the matched line. ``None``
    for a line with no numbers at all (``Cannot be Frozen``)."""

    influences: frozenset[Influence] = frozenset()
    essence_only: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "affix": self.affix.value,
            "tier": self.tier,
            "tiers": self.tiers,
            "required_level": self.required_level,
            "low": self.low,
            "high": self.high,
            "influences": sorted(i.value for i in self.influences),
            "essence_only": self.essence_only,
        }


@dataclass(frozen=True, slots=True)
class ModMatch:
    """The database's answer about one line of mod text — including "I don't know"."""

    text: str
    normalized: str
    values: tuple[float, ...]
    origin: Origin
    attribution: Attribution
    candidates: tuple[ModCandidate, ...] = ()
    ceiling: float | None = None
    """The best this mod's group can roll on this base, over every tier. ``None``
    unless the group is certain — a ceiling averaged across two possible groups is
    a number with no meaning."""

    local: bool | None = None
    """Whether this line is the **local** reading of its sentence, when that is known.

    Twenty-two sentences mean two different things depending on where they sit: a body
    armour's ``#% increased Armour`` is ``local_physical_damage_reduction_rating_+%``
    and a ring's is ``physical_damage_reduction_rating_+%``, and the trade site gives
    them different ids. Filtering the wrong one is not a near miss — measured live in
    Phase 9b, the global id matched **0** rare body armours and the local id 10 000+.

    ``None`` means the candidates disagreed, or the line was never resolved against a
    base. It is not "global": a caller that has to choose gets to see that nobody
    chose for it."""

    note: str = ""
    """Why, when the answer is not confident. Written for a developer reading a log,
    not for a panel."""

    # -- what a UI may say ---------------------------------------------------

    @property
    def group(self) -> str | None:
        if not self.attribution.is_confident:
            return None
        return self.candidates[0].group

    @property
    def affix(self) -> Affix | None:
        """Prefix or suffix — available whenever every candidate agrees.

        Deliberately *not* gated on :attr:`Attribution.is_confident`: two groups can
        be indistinguishable and still both be suffixes, and in that case the affix
        count is knowable even though the tier is not.
        """
        seen = {candidate.affix for candidate in self.candidates}
        return seen.pop() if len(seen) == 1 else None

    @property
    def tier(self) -> int | None:
        """The tier, or ``None`` when the database will not commit to one."""
        if self.attribution is not Attribution.EXACT:
            return None
        return self.candidates[0].tier

    @property
    def tiers(self) -> int | None:
        """How many tiers the group has *on this base*. The 8 in "T2 of 8"."""
        if not self.attribution.is_confident:
            return None
        return self.candidates[0].tiers

    @property
    def tier_range(self) -> tuple[int, int] | None:
        """``(best, worst)`` when the group is known but the tier is not."""
        if not self.attribution.is_confident:
            return None
        found = [candidate.tier for candidate in self.candidates]
        return min(found), max(found)

    @property
    def top_tier(self) -> bool:
        """T1, said only when it is certain."""
        return self.tier == 1

    @property
    def top_group(self) -> bool:
        """The roll is in the best tier the group reaches *on this base*.

        The question a gate actually asks, and it survives an uncertain tier: if
        every candidate is T1 then the roll is top-tier regardless of which of them
        it was.
        """
        span = self.tier_range
        return span is not None and span[1] == 1

    @property
    def value(self) -> float | None:
        """The rolled value of the first slot, if the line carried one."""
        return self.values[0] if self.values else None

    @property
    def influences(self) -> frozenset[Influence]:
        """The influence pools every candidate agrees on.

        Intersection, not union: claiming "this is a Hunter mod" when one of two
        possible mods is not a Hunter mod at all would put a fake highlight on the
        item. Empty means "not an influence mod, or not sure".
        """
        if not self.candidates:
            return frozenset()
        pools = [candidate.influences for candidate in self.candidates]
        return frozenset.intersection(*pools) if pools else frozenset()

    @property
    def is_influence_mod(self) -> bool:
        return bool(self.influences)

    def describe(self) -> str:
        """One short phrase for a 300 px panel."""
        if self.attribution is Attribution.UNKNOWN:
            return "unknown mod"
        if self.attribution is Attribution.AMBIGUOUS:
            return "tier unknown"
        tiers = self.tiers
        if self.tier is not None:
            return f"T{self.tier} of {tiers}"
        span = self.tier_range
        assert span is not None  # GROUP always has candidates
        return f"T{span[0]}-T{span[1]} of {tiers}"

    def to_json(self) -> dict[str, Any]:
        span = self.tier_range
        return {
            "text": self.text,
            "normalized": self.normalized,
            "origin": self.origin.value,
            "attribution": self.attribution.value,
            "group": self.group,
            "affix": self.affix.value if self.affix else None,
            "tier": self.tier,
            "tiers": self.tiers,
            "tier_range": list(span) if span else None,
            "top_group": self.top_group,
            "value": self.value,
            "ceiling": self.ceiling,
            "influences": sorted(i.value for i in self.influences),
            "local": self.local,
            "description": self.describe(),
            "note": self.note or None,
        }


@dataclass(frozen=True, slots=True)
class ItemMods:
    """The input to :meth:`ModDbApi.report` — deliberately not ``NormalizedItem``.

    `moddb` is core and holds no dependency at all, which means it cannot name
    ``poeapi``'s model without acquiring ``poeapi``'s whole dependency chain
    (``credentials`` and ``net``) for a module that reads one JSON file. The caller
    does the two-line translation; the field names are ``Mods``' field names so the
    translation stays obviously correct.
    """

    base_type: str
    ilvl: int = 0
    rarity: str = "rare"
    implicit: Sequence[str] = ()
    explicit: Sequence[str] = ()
    crafted: Sequence[str] = ()
    fractured: Sequence[str] = ()
    enchant: Sequence[str] = ()
    influences: Sequence[str] = ()

    def lines(self) -> list[tuple[Origin, str]]:
        return [
            *((Origin.IMPLICIT, text) for text in self.implicit),
            *((Origin.EXPLICIT, text) for text in self.explicit),
            *((Origin.FRACTURED, text) for text in self.fractured),
            *((Origin.CRAFTED, text) for text in self.crafted),
            *((Origin.ENCHANT, text) for text in self.enchant),
        ]


@dataclass(frozen=True, slots=True)
class ModReport:
    """Every line of an item, classified — plus what it adds up to."""

    base: BaseInfo | None
    matches: tuple[ModMatch, ...] = ()
    prefixes: int = 0
    suffixes: int = 0
    unattributed: int = 0
    """Affix-slot lines whose prefix/suffix side could not be decided. While this is
    non-zero the counts below are floors, not facts, and :attr:`counts_are_certain`
    is ``False``."""

    max_prefixes: int = 3
    max_suffixes: int = 3
    hybrids: int = 0
    """How many affixes were reconstructed from more than one line of text."""

    uncertain_merges: int = 0
    """How many of those merges were a *choice* rather than a deduction.

    A body armour with a small armour roll and a small life roll could be one hybrid
    prefix or two separate ones, and both readings fit every number on the item. The
    count folds them (the shorter reading), and says here that it did — because the
    difference is one open prefix, and one open prefix is what a player puts into a
    trade filter."""

    @property
    def open_prefixes(self) -> int:
        return max(0, self.max_prefixes - self.prefixes - self.unattributed)

    @property
    def open_suffixes(self) -> int:
        return max(0, self.max_suffixes - self.suffixes - self.unattributed)

    @property
    def counts_are_certain(self) -> bool:
        """Whether the open-affix numbers may be shown as numbers.

        A trade filter built on "1 open prefix" that is actually zero returns
        nothing and looks like a dead search, so the uncertain case has to be
        visible to whoever builds the filter.
        """
        return self.unattributed == 0 and self.uncertain_merges == 0 and self.base is not None

    @property
    def affixes(self) -> int:
        return self.prefixes + self.suffixes + self.unattributed

    @property
    def influence_mods(self) -> tuple[ModMatch, ...]:
        """The lines that came from an influence pool.

        Note what this is *not*: an influenced item usually carries five ordinary
        mods and one influence mod, and plenty of influenced items carry none at all
        because the influence was applied and never exploited. "Rare influence mods"
        as a highlight criterion means this list being non-empty.
        """
        return tuple(match for match in self.matches if match.is_influence_mod)

    @property
    def influences(self) -> frozenset[Influence]:
        pools: set[Influence] = set()
        for match in self.matches:
            pools |= match.influences
        return frozenset(pools)

    @property
    def confident(self) -> tuple[ModMatch, ...]:
        return tuple(m for m in self.matches if m.attribution.is_confident)

    def to_json(self) -> dict[str, Any]:
        return {
            "base": self.base.to_json() if self.base else None,
            "matches": [m.to_json() for m in self.matches],
            "prefixes": self.prefixes,
            "suffixes": self.suffixes,
            "unattributed": self.unattributed,
            "max_prefixes": self.max_prefixes,
            "max_suffixes": self.max_suffixes,
            "open_prefixes": self.open_prefixes,
            "open_suffixes": self.open_suffixes,
            "counts_are_certain": self.counts_are_certain,
            "hybrids": self.hybrids,
            "uncertain_merges": self.uncertain_merges,
            "influences": sorted(i.value for i in self.influences),
        }


@runtime_checkable
class ModDbApi(Protocol):
    """What `moddb` promises. Everything here is local, synchronous and offline."""

    def version(self) -> DbVersion:
        """What game version this database describes and when it was built."""
        ...

    def base(self, base_type: str) -> BaseInfo | None:
        """A base's class, tags and affix ceiling. ``None`` if it is not a base that
        rolls affixes — currency, a card, a map, a gem."""
        ...

    def identify(
        self,
        text: str,
        *,
        base_type: str | None = None,
        ilvl: int = 0,
        origin: Origin = Origin.EXPLICIT,
        influences: Sequence[str] = (),
    ) -> ModMatch:
        """Which mod produced ``text``, and at what tier — or an honest refusal.

        ``influences`` matters and is easy to forget: an influence mod's spawn tag is
        ``helmet_shaper``, and a Hubris Circlet *base* carries ``helmet`` and never
        ``helmet_shaper``. Omit it on an influenced item and every influence mod on
        it comes back unknown.
        """
        ...

    def report(self, item: ItemMods) -> ModReport:
        """Classify every line of an item and count its affixes."""
        ...

    def ceiling(
        self,
        text: str,
        *,
        base_type: str,
        ilvl: int = 0,
        influences: Sequence[str] = (),
    ) -> float | None:
        """The best value this mod's group can reach on this base.

        The replacement for a flat threshold: ``+80 to maximum Life`` is 56% of the
        ceiling on a body armour and 62% on a helmet, and one number cannot say that.
        """
        ...

    def trade_stat_id(
        self,
        text: str,
        *,
        origin: Origin = Origin.EXPLICIT,
        local: bool | None = None,
    ) -> str | None:
        """The trade API's opaque id for this sentence, e.g. ``explicit.stat_3299347043``.

        The bridge between the two id spaces. ``prices`` resolves mod text through
        ``/api/trade/data/stats`` at runtime; this answers the same question offline
        and from the same underlying data, so a tier lookup and a trade filter can be
        talking about the same mod and be checked against each other.

        ``local`` is :attr:`ModMatch.local` — pass it whenever you have a report,
        because twenty-two sentences have two ids and the sentence cannot say which. With
        ``None`` the global reading wins, except for the six sentences that exist
        *only* locally (``#% increased Energy Shield`` and the defence hybrids), where
        the alternative is the ``None`` that used to drop them from the query.
        """
        ...

    def game_stat_ids(self, text: str) -> tuple[str, ...]:
        """RePoE's own stat ids for this sentence, e.g. ``("base_maximum_life",)``.

        The other end of the bridge, kept so the mapping is falsifiable in both
        directions rather than asserted in one.
        """
        ...

    def groups(self, base_type: str, *, affix: Affix | None = None) -> tuple[str, ...]:
        """Every mod group that can spawn on a base. Ordered, for stable output."""
        ...
