"""Reading the committed artifact and answering questions from it.

The artifact is one JSON file of a few hundred kilobytes, built by
``scripts/build_moddb.py``. It is stored in the compact form described there — every
repeated string interned into a vocabulary, spawn-weight vectors shared between the
tiers of a group — so this file's first job is to give those integers names again.

## The four rules that make an answer true

**Spawn weights are ordered and the first match wins.** ``[{"shield": 0}, {"armour":
1000}]`` means "every armour except shields". Reading it as a set of positive tags
would say "shields too", which is a wrong answer with the shape of a right one.

**A mod's domain gates it before its weights do.** An abyss-jewel prefix lists
``default`` at weight 1000 because its *domain* has already confined it to abyss
jewels. Read the weights alone and every belt in the game rolls ``+250 to Armour``.

**A tier ladder belongs to a base.** The same ``+# to maximum Life`` has ten tiers on
a helmet and thirteen on a body armour, because three of them spawn nowhere else.
"T4 of 10" without a base type is not a fact about anything.

**A ladder also belongs to a pool.** An influence mod's tiers are that influence's
tiers, an essence-only mod is not tier 1 of the normal ladder, and a bench craft is
on its own ladder again. Mixing them is what produces "T1-T5 of 9" — two numbers read
off two different rulers.

## Attribution

:meth:`ModDatabase.identify` narrows candidates by, in order: the text, the origin
(an item's crafted array cannot hold an explicit prefix), the base's domain and spawn
tags, the item level, the rolled value against each candidate's range, and — when
:meth:`report` supplies the whole item — the other sentences a multi-line mod would
have had to write. Whatever survives is what the database is prepared to claim:

* one ladder, one tier → :data:`Attribution.EXACT`
* one ladder, several tiers → :data:`Attribution.GROUP`; the range is offered and the
  tier is withheld
* more than one ladder → :data:`Attribution.AMBIGUOUS`; nothing is claimed
* nothing survives → :data:`Attribution.UNKNOWN`, with a note saying which filter
  emptied the set

Nothing here ever picks the most likely candidate. The most likely candidate is
right most of the time, which is precisely what makes it dangerous: it is wrong
rarely enough that nobody stops trusting it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modules.moddb.backend.api import (
    Affix,
    Attribution,
    BaseInfo,
    DbVersion,
    Influence,
    ItemMods,
    ModCandidate,
    ModDbUnavailable,
    ModMatch,
    ModReport,
    Origin,
)
from modules.moddb.backend.text import negated_slots, readings

__all__ = ["DATA_FILE", "ModDatabase", "load"]

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "moddb.json"

SCHEMA = 2
"""Phase 9b. Schema 1 put the **global** trade stat id on local mods — a body armour's
``#% increased Armour`` searched as ``explicit.stat_2866361420`` matched 0 listings
live where the local id matched 10 000+ — so a schema-1 artifact is not read at all.
Refusing it is deliberate: the old one does not fail, it answers with a query that
quietly returns nothing, which is the failure this project keeps having to unpick."""

# Which RePoE domain a line of each origin may have come from. This is the cheapest
# and most reliable disambiguator in the whole module, and it costs nothing: the item
# endpoints hand back `implicitMods`, `explicitMods` and `craftedMods` as separate
# arrays, so a `+# to maximum Life` in the crafted array is simply never a candidate
# for the explicit life prefix.
_DOMAINS_BY_ORIGIN: Mapping[Origin, frozenset[str]] = {
    Origin.EXPLICIT: frozenset(
        {"item", "flask", "abyss_jewel", "tincture", "jewel", "cluster_jewel"}
    ),
    Origin.FRACTURED: frozenset({"item", "abyss_jewel", "jewel"}),
    # `unveiled` belongs with `crafted`, not with `explicit`. An unveiled mod was put
    # there by Jun's bench and the item endpoints file it under `craftedMods`.
    # Allowing it as an explicit candidate made `+58 to maximum Life` on an amulet
    # ambiguous between a dropped T5 and an unveiled mod nobody has ever seen on a
    # dropped rare — noise, and expensive noise, since it withholds a real tier.
    Origin.CRAFTED: frozenset({"crafted", "unveiled"}),
    # Implicits come off the base and are not affixes; the database carries no
    # implicit mods at all, so nothing will ever match. Stated rather than left to
    # fall out, because "no candidates" and "we don't model this" are different
    # answers and the note says which one happened.
    Origin.IMPLICIT: frozenset(),
    Origin.ENCHANT: frozenset(),
}

_JEWEL_CLASSES = frozenset({"Jewel", "AbyssJewel"})
"""Rare jewels take two prefixes and two suffixes, not three of each."""

MAX_AFFIXES: Mapping[str, tuple[int, int]] = {
    "normal": (0, 0),
    "magic": (1, 1),
    "rare": (3, 3),
    "unique": (0, 0),
}
"""Affix slots by rarity. Uniques are excluded on purpose — their mods are fixed by
the unique, not rolled, so "two open prefixes" is not a thing you can say about one."""


@dataclass(frozen=True, slots=True)
class _Mod:
    group: int
    affix: int
    level: int
    domain: int
    spawn: int
    influence: int
    essence: bool
    lines: tuple[tuple[int, tuple[tuple[float, float], ...]], ...]
    local: int = 0
    """Bitmask over :attr:`lines`: does this line want the *local* trade stat id?

    A property of the mod, never of the sentence. ``+# to maximum Energy Shield`` is
    ``local_energy_shield`` on a chest and ``energy_shield`` on a ring, and the trade
    site numbers those differently, so the sentence alone cannot answer it."""

    def is_local(self, position: int) -> bool:
        return bool(self.local & (1 << position))

    @property
    def pool(self) -> tuple[int, bool]:
        """What ladder this mod belongs to: its influence mask and essence flag."""
        return self.influence, self.essence


_UNGATED_DOMAINS = frozenset({"crafted", "unveiled"})
"""Domains whose applicability is decided somewhere this database cannot see.

A bench craft's ``spawn_weights`` are empty — what a bench may put on an item is in
the crafting bench tables, not in ``mods.json`` — and an unveiled mod is offered by
Jun rather than rolled. Treating "no spawn weight matched" as "cannot appear here"
would silently drop every crafted mod on every item, and the affix count would then
be wrong on exactly the items a player has already invested in."""


@dataclass(frozen=True, slots=True)
class _Base:
    name: str
    item_class: int
    tags: tuple[int, ...]
    drop_level: int
    top_affix_level: int
    domain: int

    @property
    def key(self) -> tuple[int, ...]:
        return (self.domain, *self.tags)


class ModDatabase:
    """The artifact, loaded. Immutable, thread-safe, and roughly 20 MB of RAM lighter
    than the upstream files it came from."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        schema = document.get("schema")
        if schema != SCHEMA:
            raise ModDbUnavailable(
                f"mod database schema {schema!r} is not the {SCHEMA} this build reads; "
                "re-run scripts/build_moddb.py"
            )
        vocab = document["vocab"]
        self._tags: list[str] = list(vocab["tags"])
        self._tag_index = {name: i for i, name in enumerate(self._tags)}
        self._groups: list[str] = list(vocab["groups"])
        self._group_index = {name: i for i, name in enumerate(self._groups)}
        self._texts: list[str] = list(vocab["texts"])
        self._text_index = {text: i for i, text in enumerate(self._texts)}
        self._folded_index: dict[str, int] = {}
        for index, text in enumerate(self._texts):
            self._folded_index.setdefault(text.casefold(), index)
        # How many values each line carries is the number of ``#`` in its text, so
        # the flat encoding stores no arity and this table is how it is recovered.
        arity = [text.count("#") for text in self._texts]
        # ...and which of those slots the sentence spells with a leading minus. The
        # build step stores ``-(4-9) to Total Mana Cost of Skills`` as the positive
        # range ``(4, 9)``; every consumer of this module wants the ``(-9, -4)`` the
        # item actually displays, so the flip happens once, here, on the way in.
        self._negated = [negated_slots(text) for text in self._texts]
        self._domains: list[str] = list(vocab["domains"])
        self._classes: list[str] = list(vocab["classes"])
        self._influences: list[str] = list(vocab["influences"])
        self._influence_tags: list[str] = list(vocab["influence_tags"])

        self._spawn: list[tuple[tuple[int, int], ...]] = [
            tuple((code >> 1, code & 1) for code in vector) for vector in document["spawn"]
        ]
        self._can_roll: list[bool] = [
            any(weight for _tag, weight in vector) for vector in self._spawn
        ]

        self._mods: list[_Mod] = [
            _read_mod(record, arity, self._negated) for record in document["mods"]
        ]

        self._by_text: dict[int, list[int]] = {}
        for index, mod in enumerate(self._mods):
            for text_index, _slots in mod.lines:
                self._by_text.setdefault(text_index, []).append(index)

        self._bases: dict[str, _Base] = {}
        for name, record in document["bases"].items():
            self._bases[name.casefold()] = _Base(
                name=name,
                item_class=int(record[0]),
                tags=tuple(int(t) for t in record[1]),
                drop_level=int(record[2]),
                top_affix_level=int(record[3]),
                domain=int(record[4]),
            )

        self._trade: dict[int, tuple[int, tuple[str, ...]]] = {
            int(key): (int(value[0]), tuple(str(v) for v in value[1:]))
            for key, value in document["trade"].items()
        }
        # The 22 sentences GGG publishes twice, in the local reading. Consulted only
        # for a line whose mod said it is local; nothing falls back into it by
        # accident, because a global mod searched by a local id is the same bug
        # pointing the other way.
        self._trade_local: dict[int, tuple[int, tuple[str, ...]]] = {
            int(key): (int(value[0]), tuple(str(v) for v in value[1:]))
            for key, value in document.get("trade_local", {}).items()
        }
        self._trade_origins = ("explicit", "implicit", "crafted", "fractured", "enchant")
        self._game_stats: dict[int, tuple[str, ...]] = {
            int(key): tuple(str(v) for v in value)
            for key, value in document["game_stats"].items()
        }

        source = document["source"]
        counts = document["counts"]
        self._version = DbVersion(
            game_version=str(source.get("game_version") or "unknown"),
            generated_at=_parse_time(source.get("generated_at")),
            source=str(source.get("url") or ""),
            mods=int(counts.get("mods", len(self._mods))),
            bases=int(counts.get("bases", len(self._bases))),
            texts=int(counts.get("texts", len(self._texts))),
            schema=SCHEMA,
        )

        self._spawn_cache: dict[tuple[int, int, tuple[int, ...]], bool] = {}
        self._ladder_cache: dict[tuple[int, int, tuple[int, bool], tuple[int, ...]], _Ladder] = {}

    # -- ModDbApi ------------------------------------------------------------

    def version(self) -> DbVersion:
        return self._version

    def base(self, base_type: str) -> BaseInfo | None:
        found = self._bases.get((base_type or "").strip().casefold())
        if found is None:
            return None
        return BaseInfo(
            name=found.name,
            item_class=self._classes[found.item_class],
            tags=frozenset(self._tags[t] for t in found.tags),
            drop_level=found.drop_level,
            top_affix_level=found.top_affix_level,
        )

    def identify(
        self,
        text: str,
        *,
        base_type: str | None = None,
        ilvl: int = 0,
        origin: Origin = Origin.EXPLICIT,
        influences: Sequence[str] = (),
        alongside: frozenset[int] | None = None,
    ) -> ModMatch:
        """``alongside`` is the whole-item context, and it is what fixes hybrids.

        It is the set of normalized texts the item carries **for this origin**. A mod
        that writes two sentences can only be the answer if both sentences are on the
        item: an Iron Hat showing ``+26 to Armour`` and no life line cannot be the
        armour-and-life hybrid, however well the number fits. Per-line attribution
        cannot see that; :meth:`report` passes it in, which is why the same text is
        often ``ambiguous`` alone and ``exact`` in context.
        """
        return self._resolve(
            text,
            base_type=base_type,
            ilvl=ilvl,
            origin=origin,
            influences=influences,
            alongside=alongside,
        )[0]

    def _resolve(
        self,
        text: str,
        *,
        base_type: str | None,
        ilvl: int,
        origin: Origin,
        influences: Sequence[str],
        alongside: frozenset[int] | None,
    ) -> tuple[ModMatch, tuple[int, ...], frozenset[Affix]]:
        """:meth:`identify`, plus the mod indices that survived.

        The indices never leave this module — they are artifact row numbers and mean
        nothing outside it — but :meth:`report` needs them: two lines belong to the
        same affix when one surviving *mod* explains both, and a public
        :class:`ModMatch` deliberately does not expose which mod that was.

        A negatively-rolled line is asked twice, because the game data spells its sign
        two ways and **both spellings are real sentences in the same group**:
        ``IncreaseFlatManaCost`` writes ``# to Total Mana Cost of Skills`` for its
        ``-4`` and ``-5`` tiers and ``-# to Total Mana Cost of Skills`` for the rest.
        Taking the first spelling that merely *exists* therefore answers "nothing that
        can spawn here produces this text" about a mod the artifact carries nine tiers
        of, which is how every reduced-cost roll in the game came back unknown. The
        first reading that produces an actual answer wins; only if neither does is the
        refusal reported, and then against the sentence as written.
        """
        attempts = [
            self._resolve_reading(
                text,
                index,
                normalized,
                values,
                base_type=base_type,
                ilvl=ilvl,
                origin=origin,
                influences=influences,
                alongside=alongside,
            )
            for normalized, values in readings(text)
            if (index := self._lookup(normalized)) is not None
        ]
        for attempt in attempts:
            if attempt[0].attribution is not Attribution.UNKNOWN:
                return attempt
        if attempts:
            return attempts[0]
        return self._unreadable(text, origin)

    def _unreadable(self, text: str, origin: Origin) -> tuple[
        ModMatch, tuple[int, ...], frozenset[Affix]
    ]:
        normalized, values = readings(text)[0]
        blank = ModMatch(
            text=text,
            normalized=normalized,
            values=tuple(low for low, _high in values),
            origin=origin,
            attribution=Attribution.UNKNOWN,
        )
        if not _DOMAINS_BY_ORIGIN.get(origin, frozenset()):
            return _with_note(
                blank, f"{origin.value} lines are not affixes; the database carries none"
            ), (), frozenset()
        return _with_note(
            blank,
            "no mod in the database renders this text — the artifact may be a "
            "league out of date",
        ), (), frozenset()

    def _resolve_reading(
        self,
        text: str,
        text_index: int,
        normalized: str,
        values: tuple[tuple[float, float], ...],
        *,
        base_type: str | None,
        ilvl: int,
        origin: Origin,
        influences: Sequence[str],
        alongside: frozenset[int] | None,
    ) -> tuple[ModMatch, tuple[int, ...], frozenset[Affix]]:
        """:meth:`_resolve` for one already-chosen spelling of the sentence."""
        rolled = tuple(low for low, _high in values)
        blank = ModMatch(
            text=text,
            normalized=normalized,
            values=rolled,
            origin=origin,
            attribution=Attribution.UNKNOWN,
        )

        allowed = _DOMAINS_BY_ORIGIN.get(origin, frozenset())
        if not allowed:
            return _with_note(
                blank,
                f"{origin.value} lines are not affixes; the database carries none",
            ), (), frozenset()

        base = self._bases.get((base_type or "").strip().casefold())
        if base_type and base is None:
            return _with_note(
                blank, f"{base_type!r} is not a base that rolls affixes"
            ), (), frozenset()
        if base is not None and influences:
            base = self._influenced(base, influences)

        survivors: list[int] = []
        loose: list[int] = []
        # Which mods could put this sentence here *at all* — text, origin and base,
        # and nothing about the roll. Locality is read off this set rather than off
        # the survivors on purpose: which id to search does not depend on how well the
        # mod rolled, and an unattributable line is still a line the player can tick.
        # Attribution fails on about one affix line in five; letting that failure also
        # lose the local/global choice would put the wrong id on exactly the lines
        # nobody can check.
        spawnable: list[int] = []
        # Everything the base *and* the item level allow, before the roll is compared
        # against anything. This is the set :meth:`_summed` reasons over — the game
        # adds two affixes granting the same stat into one displayed line, and the two
        # that did it are each individually too small to fit the number on screen.
        placeable: list[int] = []
        rejected_by_value = 0
        for index in self._by_text.get(text_index, ()):
            mod = self._mods[index]
            if self._domains[mod.domain] not in allowed:
                continue
            if base is not None and not self._spawns(mod, base):
                continue
            spawnable.append(index)
            if ilvl and mod.level > ilvl and self._domains[mod.domain] not in _UNGATED_DOMAINS:
                # `required_level` on a bench craft is not an item-level gate the way
                # it is on a dropped affix, and the fixtures prove it: a live ilvl-69
                # belt carries a crafted `+13% to Lightning and Chaos Resistances`
                # whose only matching tier is marked level 81. Applying the filter
                # there turned a mod the player paid for into "unknown".
                continue
            placeable.append(index)
            if not _fits(mod, text_index, values):
                rejected_by_value += 1
                continue
            loose.append(index)
            if alongside is None or {t for t, _s in mod.lines} <= alongside:
                survivors.append(index)

        local = self._locality(spawnable, text_index)

        if not loose:
            reason = (
                "the rolled value is outside every tier's range"
                if rejected_by_value
                else "nothing that can spawn here produces this text"
            )
            return _with_note(replace(blank, local=local), reason), (), frozenset()

        if not survivors:
            # Context narrows; it never eliminates. If every candidate writes a line
            # the item does not show, the likeliest explanation is that *this*
            # database missed a sentence, not that the mod is impossible — so the
            # unfiltered answer is given back rather than an "unknown" that is more
            # confident about its own ignorance than the evidence supports.
            unfiltered = _with_note(
                self._decide(blank, loose, base, text_index, local),
                "every candidate also writes a line this item does not show; "
                "attributed without that check",
            )
            return unfiltered, tuple(loose), frozenset()

        decided = self._decide(blank, survivors, base, text_index, local)
        return (
            decided,
            tuple(survivors),
            self._summed_sides(decided, placeable, text_index, values, alongside),
        )

    def report(self, item: ItemMods) -> ModReport:
        base = self.base(item.base_type)
        lines = item.lines()
        # Which sentences the item carries, per origin. This is what lets a hybrid be
        # ruled in or out: a mod that writes two lines must find both of them here.
        present: dict[Origin, set[int]] = {}
        for origin, text in lines:
            # Every spelling the line could be filed under, not just the first that
            # exists. A negative roll has two, they are the same sentence, and the
            # hybrid check is a subset test — recording only one of them would rule
            # out a hybrid because it wrote its line the other way round.
            for normalized, _values in readings(text):
                found = self._lookup(normalized)
                if found is not None:
                    present.setdefault(origin, set()).add(found)
        resolved = [
            self._resolve(
                text,
                base_type=item.base_type,
                ilvl=item.ilvl,
                origin=origin,
                influences=item.influences,
                alongside=frozenset(present.get(origin, ())),
            )
            for origin, text in lines
        ]
        affix_lines = [
            (origin, match, survivors)
            for (origin, _text), (match, survivors, _sides) in zip(lines, resolved, strict=True)
            if origin.is_affix
        ]
        prefixes, suffixes, unattributed, hybrids, uncertain = self._count(affix_lines)

        max_prefixes, max_suffixes = MAX_AFFIXES.get(item.rarity.lower(), (3, 3))
        if base is not None and base.item_class in _JEWEL_CLASSES and item.rarity.lower() == "rare":
            max_prefixes = max_suffixes = 2

        # A two-affix reading of a line costs one more slot than the one-affix
        # reading, and the game only has six. Asked per line the sum is nearly always
        # *conceivable*; asked against what the rest of the item has already spent it
        # is often impossible, and an item with no free prefix cannot be hiding a
        # second prefix inside a displayed total. Counted from the same numbers
        # `ModReport.open_prefixes` publishes, so the refusal and the open-affix
        # filter can never disagree about how full the item is.
        free = {
            Affix.PREFIX: max(0, max_prefixes - prefixes - unattributed),
            Affix.SUFFIX: max(0, max_suffixes - suffixes - unattributed),
        }
        matches = tuple(
            _as_summed(match) if any(free[side] for side in sides) else match
            for match, _survivors, sides in resolved
        )

        return ModReport(
            base=base,
            matches=matches,
            prefixes=prefixes,
            suffixes=suffixes,
            unattributed=unattributed,
            max_prefixes=max_prefixes,
            max_suffixes=max_suffixes,
            hybrids=hybrids,
            uncertain_merges=uncertain,
        )

    def ceiling(
        self,
        text: str,
        *,
        base_type: str,
        ilvl: int = 0,
        influences: Sequence[str] = (),
    ) -> float | None:
        return self.identify(
            text, base_type=base_type, ilvl=ilvl, influences=influences
        ).ceiling

    def trade_stat_id(
        self,
        text: str,
        *,
        origin: Origin = Origin.EXPLICIT,
        local: bool | None = None,
    ) -> str | None:
        index, _normalized, _values = self._reading(text)
        if index is None:
            return None
        found = self._trade_local.get(index) if local else self._trade.get(index)
        if found is None and local is None:
            # `local=None` is "nobody asked the mod" — a bare text lookup from the CLI,
            # or an item with no `moddb` report. The local table is then a fallback and
            # not a preference: six common sentences (`98% increased Energy Shield`
            # and the defence hybrids) exist **only** in the local reading, and
            # answering "no id" for those is how they were silently dropped from
            # queries before.
            found = self._trade_local.get(index)
        if found is None:
            return None
        mask, ids = found
        wanted = origin.value
        if wanted not in self._trade_origins:
            return None
        position = self._trade_origins.index(wanted)
        if not mask & (1 << position):
            return None
        if len(ids) == 1:
            return f"{wanted}.{ids[0]}"
        # Several distinct numbers: they are listed in ``_trade_origins`` order for
        # whichever bits are set, so count the set bits below this one.
        rank = bin(mask & ((1 << position) - 1)).count("1")
        return f"{wanted}.{ids[rank]}"

    def game_stat_ids(self, text: str) -> tuple[str, ...]:
        index, _normalized, _values = self._reading(text)
        if index is None:
            return ()
        return self._game_stats.get(index, ())

    def groups(self, base_type: str, *, affix: Affix | None = None) -> tuple[str, ...]:
        base = self._bases.get((base_type or "").strip().casefold())
        if base is None:
            return ()
        wanted = None if affix is None else (0 if affix is Affix.PREFIX else 1)
        found = {
            self._groups[mod.group]
            for mod in self._mods
            # Naturally rollable only. Bench crafts are ungated here — what a bench
            # may add lives in the crafting data, not in this file — so including
            # them would answer "what could this base end up with", a different and
            # much longer question than the one being asked.
            if mod.domain == base.domain
            and (wanted is None or mod.affix == wanted)
            and self._spawns(mod, base)
        }
        return tuple(sorted(found))

    # -- internals -----------------------------------------------------------

    def _influenced(self, base: _Base, influences: Sequence[str]) -> _Base:
        """The base as an *influenced* item sees it.

        This is not a detail. An influence mod's spawn tag is ``helmet_shaper``, and
        a Hubris Circlet base carries ``helmet`` and never ``helmet_shaper`` — the
        tag appears only once the item has the influence. Without this, every
        influence mod in the database is unmatchable and the "rare influence mods"
        highlight can never fire, which is the sort of bug that looks like the
        feature simply never triggering.
        """
        wanted = {name.strip().casefold() for name in influences if name}
        internal = [
            self._influence_tags[position]
            for position, name in enumerate(self._influences)
            if name in wanted
        ]
        if not internal:
            return base
        extra = {
            found
            for tag in (self._tags[index] for index in base.tags)
            for suffix in internal
            if (found := self._tag_index.get(f"{tag}_{suffix}")) is not None
        }
        if not extra:
            return base
        return _Base(
            name=base.name,
            item_class=base.item_class,
            tags=tuple(sorted({*base.tags, *extra})),
            drop_level=base.drop_level,
            top_affix_level=base.top_affix_level,
            domain=base.domain,
        )

    def _spawns(self, mod: _Mod, base: _Base) -> bool:
        """Can ``mod`` appear on ``base``? Two gates, and both are needed.

        The **domain** gate first: an abyss-jewel prefix lists ``default`` at weight
        1000 because its domain has already restricted it to abyss jewels, so spawn
        weights read on their own say every belt in the game rolls ``+250 to
        Armour``. That was a real wrong answer this check exists to stop.

        Then the **spawn weights**, first matching tag wins.
        """
        key = (mod.spawn, mod.domain, base.key)
        cached = self._spawn_cache.get(key)
        if cached is None:
            cached = self._domain_allows(mod, base) and self._weights_allow(mod, base)
            self._spawn_cache[key] = cached
        return cached

    def _domain_allows(self, mod: _Mod, base: _Base) -> bool:
        return mod.domain == base.domain or self._domains[mod.domain] in _UNGATED_DOMAINS

    def _weights_allow(self, mod: _Mod, base: _Base) -> bool:
        vector = self._spawn[mod.spawn]
        if not self._can_roll[mod.spawn]:
            # No positive weight *anywhere* — a bench craft's table is
            # ``[{"default": 0}]``, because what a bench may put on an item lives in
            # the crafting bench data, not here. For a `crafted` or `unveiled` mod
            # that means "decided elsewhere, allow it"; for an `item`-domain mod it
            # means the mod is unreachable by dropping, and several disabled hybrids
            # in the file are exactly that. Treating those as universal is how
            # ``#% increased maximum Life`` acquired a phantom body-armour tier.
            return self._domains[mod.domain] in _UNGATED_DOMAINS
        tags = set(base.tags)
        for tag, weight in vector:
            if tag in tags:
                return bool(weight)
        return False

    def _ladder(self, mod: _Mod, base: _Base | None) -> _Ladder:
        """The tier ladder of ``mod``'s group and pool, on ``base``.

        Without a base every mod of the group is on the ladder, which overcounts —
        so a match with no base type is still allowed a tier, but the caller has been
        told less than it thinks. Callers inside this module always pass the base
        when the item had one.
        """
        key = (mod.group, mod.domain, mod.pool, base.key if base else ())
        cached = self._ladder_cache.get(key)
        if cached is None:
            levels = sorted(
                {
                    other.level
                    for other in self._mods
                    if other.group == mod.group
                    and other.domain == mod.domain
                    and other.pool == mod.pool
                    and (base is None or self._spawns(other, base))
                },
                reverse=True,
            )
            cached = _Ladder(levels={level: rank + 1 for rank, level in enumerate(levels)})
            self._ladder_cache[key] = cached
        return cached

    def _candidate(self, mod: _Mod, text_index: int, base: _Base | None) -> ModCandidate:
        ladder = self._ladder(mod, base)
        slots = _slots_for(mod, text_index)
        low = slots[0][0] if slots else None
        high = slots[0][1] if slots else None
        return ModCandidate(
            group=self._groups[mod.group],
            affix=Affix.PREFIX if mod.affix == 0 else Affix.SUFFIX,
            tier=ladder.tier(mod.level),
            tiers=ladder.size,
            required_level=mod.level,
            low=low,
            high=high,
            influences=frozenset(
                Influence(self._influences[bit])
                for bit in range(len(self._influences))
                if mod.influence & (1 << bit)
            ),
            essence_only=mod.essence,
        )

    def _summed_sides(
        self,
        match: ModMatch,
        placeable: Sequence[int],
        text_index: int,
        values: Sequence[tuple[float, float]],
        alongside: frozenset[int] | None,
    ) -> frozenset[Affix]:
        """Which **extra affix slot** a two-affix reading of this line would need.

        Path of Exile does not print a line per affix; it prints a line per *stat*.
        Two affixes granting the same stat are added together before the tooltip is
        drawn, and the number on screen is then a roll no single mod ever made. Phase
        9b measured 2 of 104 lines on the live-rare fixture like this — ``+161 to
        Evasion Rating`` on a Grizzly Pelt is a P2 hybrid plus a P3 prefix — and the
        failure was not the miss but the **confidence**: asked about the sum this file
        found the one tier whose range contains 161, answered ``T1 of 8``, and the
        panel pre-ticked it as a top-tier roll.

        Empty means no two-affix reading exists. A non-empty result is a *question*,
        answered by :meth:`report` against the item's remaining affix slots, because
        the constraint that settles it is a whole-item fact and not a per-line one.

        Four conditions, each a fact rather than a preference:

        * **Different groups.** An item cannot carry two affixes of the same group, so
          a pair drawn from one group is not a reading the game could have produced.
        * **Both fully written.** Every *other* sentence each mod would have had to
          write is on the item — the same ``alongside`` deduction that fixes hybrids
          everywhere else in this file. It is also why this returns nothing at all
          without item context: :meth:`identify` on a bare sentence has no way to know
          whether the hybrid's other line is there, and a database that answered
          "possibly summed" to every question would be refusing to read the game.
        * **The arithmetic works**, slot by slot: the displayed number lies inside
          ``(low_a + low_b, high_a + high_b)``.
        * **The pair contains the single reading's own side**, so the difference
          between the two readings is exactly one more affix, and this can name which
          side it would have to come from.

        Note what is *not* required: that no single mod fits. A number two readings
        both explain is uncertain whichever is likelier, and preferring the likelier
        one is the habit this module exists in order not to have.
        """
        single = match.affix
        if alongside is None or not values or single is None:
            return frozenset()
        candidates: list[tuple[_Mod, tuple[tuple[float, float], ...]]] = []
        for index in placeable:
            mod = self._mods[index]
            if not {t for t, _s in mod.lines} <= alongside:
                continue
            slots = _slots_for(mod, text_index)
            if len(slots) != len(values):
                continue
            candidates.append((mod, slots))

        needed: set[Affix] = set()
        for position, (first, first_slots) in enumerate(candidates):
            for second, second_slots in candidates[position + 1 :]:
                if first.group == second.group:
                    continue
                if not all(
                    low_a + low_b <= rolled <= high_a + high_b
                    for (low_a, high_a), (low_b, high_b), (rolled, _same) in zip(
                        first_slots, second_slots, values, strict=True
                    )
                ):
                    continue
                sides = [_side(first), _side(second)]
                if single not in sides:
                    continue
                sides.remove(single)
                needed.add(sides[0])
        return frozenset(needed)

    def _decide(
        self,
        blank: ModMatch,
        survivors: Sequence[int],
        base: _Base | None,
        text_index: int,
        local: bool | None = None,
    ) -> ModMatch:
        """Turn the surviving mods into a claim, or into a refusal to make one.

        The unit of comparison is a **ladder** — ``(group, domain, pool)`` — not a
        group name. Two mods can share a group and still be on different ladders: a
        dropped affix and a bench craft of the same group, an ordinary roll and an
        essence-only one, a Shaper tier and a plain one. Their tier numbers are then
        counted from different places, and "T1-T5 of 9" is not a range, it is two
        numbers from two rulers printed side by side. So candidates spanning
        ladders are :data:`Attribution.AMBIGUOUS`, whatever their groups say.
        """
        exemplars = [self._mods[index] for index in survivors]
        candidates = tuple(self._candidate(mod, text_index, base) for mod in exemplars)
        ladders = {(mod.group, mod.domain, mod.pool) for mod in exemplars}
        if len(ladders) > 1:
            groups = sorted({candidate.group for candidate in candidates})
            reason = (
                f"{len(groups)} groups render this text here: " + ", ".join(groups)
                if len(groups) > 1
                else f"{len(ladders)} separate {groups[0]} ladders render this text here "
                "(a dropped, crafted, essence or influence variant), and their tier "
                "numbers are not comparable"
            )
            return ModMatch(
                text=blank.text,
                normalized=blank.normalized,
                values=blank.values,
                origin=blank.origin,
                attribution=Attribution.AMBIGUOUS,
                candidates=candidates,
                local=local,
                note=reason,
            )

        tiers = {candidate.tier for candidate in candidates}
        attribution = Attribution.EXACT if len(tiers) == 1 else Attribution.GROUP
        note = (
            ""
            if attribution is Attribution.EXACT
            else f"{len(tiers)} tiers of {candidates[0].group} could have rolled this value"
        )
        return ModMatch(
            text=blank.text,
            normalized=blank.normalized,
            values=blank.values,
            origin=blank.origin,
            attribution=attribution,
            candidates=candidates,
            local=local,
            ceiling=self._ceiling(exemplars[0], base, text_index),
            note=note,
        )

    def _locality(self, candidates: Sequence[int], text_index: int) -> bool | None:
        """Whether this line wants the local trade stat id, when every candidate agrees.

        ``candidates`` is everything the text, the origin and the base allow — *not*
        the attribution survivors. The roll has no bearing on which stat id the
        sentence belongs to, and about one affix line in five never gets attributed
        at all; making the id depend on that would lose the choice on exactly the
        lines nobody can double-check.

        Unanimity is the bar for the same reason it is everywhere else in this file:
        two mods that disagree here are two different stats, and picking one puts a
        filter on the query that matches a mod the item does not have. Disagreement is
        ``None``, which sends the caller to the global id — what Phase 9 did — so an
        unforeseen case is no worse than before rather than newly wrong.
        """
        seen: set[bool] = set()
        for index in candidates:
            mod = self._mods[index]
            for position, (line, _slots) in enumerate(mod.lines):
                if line == text_index:
                    seen.add(mod.is_local(position))
                    break
        return seen.pop() if len(seen) == 1 else None

    def _reading(self, text: str) -> tuple[int | None, str, tuple[tuple[float, float], ...]]:
        """The vocabulary's index for a line, and the reading that found it.

        A line whose value is negative has two spellings and only the artifact knows
        which one it was built with; :func:`readings` offers both and this takes the
        first that exists. When neither does, the first reading is returned so the
        ``unknown`` note still quotes the sentence the way it was written.
        """
        options = readings(text)
        for normalized, values in options:
            found = self._lookup(normalized)
            if found is not None:
                return found, normalized, values
        return None, options[0][0], options[0][1]

    def _lookup(self, normalized: str) -> int | None:
        """The text's index, with a case-insensitive fallback.

        Exact first, because GGG's own spelling is what the database was built from
        and a miss is real information. The fallback exists because a single letter
        of capitalization drift between a patch and this artifact would otherwise
        turn a whole mod into "unknown", and losing a tier to a capital ``F`` is not
        a trade anybody would make deliberately.
        """
        found = self._text_index.get(normalized)
        if found is not None:
            return found
        return self._folded_index.get(normalized.casefold())

    def _ceiling(self, exemplar: _Mod, base: _Base | None, text_index: int) -> float | None:
        """The best first-slot value this group and pool can reach here, over every tier.

        Not the matched tier's maximum: the question a consumer asks is "is this a
        good roll for this base", and that is measured against what the base can do,
        not against what this tier can do. Restricted to the same pool, because a
        Shaper-only tier is not something an ordinary rare could have rolled and
        comparing against it would make every normal roll look mediocre.

        **"Best" is not "largest."** ``-9 to Total Mana Cost of Skills`` is a better
        roll than ``-4``, so on a ladder whose reachable values are negative the
        ceiling is the *minimum*. Taking the maximum there returned the worst tier in
        the game and called it the thing to measure against — a ceiling that makes a
        top roll look like 44% of what was available.
        """
        reachable: list[tuple[float, float]] = []
        for mod in self._mods:
            if mod.group != exemplar.group or mod.domain != exemplar.domain:
                continue
            if mod.pool != exemplar.pool:
                continue
            if base is not None and not self._spawns(mod, base):
                continue
            slots = _slots_for(mod, text_index)
            if slots:
                reachable.append(slots[0])
        if not reachable:
            return None
        if all(high <= 0 for _low, high in reachable):
            return min(low for low, _high in reachable)
        return max(high for _low, high in reachable)

    def _count(
        self, affix_lines: Sequence[tuple[Origin, ModMatch, tuple[int, ...]]]
    ) -> tuple[int, int, int, int, int]:
        """Fold lines into affixes, then count the sides.

        A hybrid mod writes two sentences and occupies **one** affix slot, so counting
        lines overcounts every ``+# to maximum Life / #% increased maximum Life`` item
        by one. Two lines belong together when one *surviving* mod explains both — the
        survivors, not merely the texts, because a body armour showing ``+494 to
        Armour`` and ``+154 to maximum Life`` matches the armour-and-life hybrid on
        text and misses it by a hundred points on value. Merging those would report
        one prefix where there are two, and "one open prefix" is the number a player
        would then paste into a trade filter.
        """
        components: list[list[ModMatch]] = []
        claimed: set[int] = set()
        uncertain = 0
        for position, (origin, match, survivors) in enumerate(affix_lines):
            if position in claimed:
                continue
            component = [match]
            claimed.add(position)
            # Could this line stand on its own? If some survivor writes only this
            # sentence, then a merge is a *choice* between two readings, not a
            # deduction — and the affix count that follows is a guess.
            standalone = any(len(self._mods[index].lines) == 1 for index in survivors)
            # Only a multi-line mod can absorb another line, and it has to be one of
            # *this* line's survivors — a mod already ruled out by base, level or
            # value is not evidence about anything.
            shared = {
                index for index in survivors if len(self._mods[index].lines) > 1
            }
            if shared:
                for other_position in range(position + 1, len(affix_lines)):
                    if other_position in claimed:
                        continue
                    other_origin, other, other_survivors = affix_lines[other_position]
                    if other_origin is not origin:
                        continue
                    if shared & set(other_survivors):
                        claimed.add(other_position)
                        component.append(other)
                        if standalone or any(
                            len(self._mods[index].lines) == 1 for index in other_survivors
                        ):
                            uncertain += 1
            components.append(component)

        prefixes = suffixes = unattributed = hybrids = 0
        for component in components:
            if len(component) > 1:
                hybrids += 1
            sides = {match.affix for match in component if match.affix is not None}
            if len(sides) != 1:
                unattributed += 1
            elif sides.pop() is Affix.PREFIX:
                prefixes += 1
            else:
                suffixes += 1
        return prefixes, suffixes, unattributed, hybrids, uncertain


@dataclass(frozen=True, slots=True)
class _Ladder:
    levels: Mapping[int, int]

    @property
    def size(self) -> int:
        return len(self.levels)

    def tier(self, level: int) -> int:
        return self.levels.get(level, self.size)


def _read_mod(
    record: Sequence[Any], arity: Sequence[int], negated: Sequence[Sequence[bool]]
) -> _Mod:
    """One mod record, with its ``[text, lo, hi, lo, hi, text, ...]`` line list undone.

    ``negated`` turns the artifact's units into the item's. A sentence that spells its
    own minus (``-# to Total Mana Cost of Skills``) stores ``(4, 9)`` and means
    ``(-9, -4)``, and every question this module answers — does the roll fit, what is
    the best this base reaches, which way is good — is asked about the number on the
    tooltip. Flipping once on load is what stops each of those from having to know.
    """
    flat = record[7]
    lines: list[tuple[int, tuple[tuple[float, float], ...]]] = []
    position = 0
    while position < len(flat):
        text_index = int(flat[position])
        position += 1
        signs = negated[text_index]
        slots: list[tuple[float, float]] = []
        for slot in range(arity[text_index]):
            low, high = float(flat[position]), float(flat[position + 1])
            slots.append((-high, -low) if slot < len(signs) and signs[slot] else (low, high))
            position += 2
        lines.append((text_index, tuple(slots)))
    return _Mod(
        group=int(record[0]),
        affix=int(record[1]),
        level=int(record[2]),
        domain=int(record[3]),
        spawn=int(record[4]),
        influence=int(record[5]),
        essence=bool(record[6]),
        lines=tuple(lines),
        local=int(record[8]) if len(record) > 8 else 0,
    )


def _slots_for(mod: _Mod, text_index: int) -> tuple[tuple[float, float], ...]:
    for index, slots in mod.lines:
        if index == text_index:
            return slots
    return ()


def _fits(mod: _Mod, text_index: int, values: Sequence[tuple[float, float]]) -> bool:
    """Whether the rolled values sit inside this mod's range for that line.

    A line with no numbers (``Cannot be Frozen``) fits trivially — there is nothing
    to compare, and rejecting it would lose the mod entirely.
    """
    slots = _slots_for(mod, text_index)
    if not slots or len(slots) != len(values):
        return True
    for (low, high), (rolled, _same) in zip(slots, values, strict=True):
        if not (low <= rolled <= high):
            return False
    return True


def _side(mod: _Mod) -> Affix:
    return Affix.PREFIX if mod.affix == 0 else Affix.SUFFIX


SUMMED_NOTE = (
    "two affixes could have been added into this one displayed line and the item has "
    "a free slot for the second, so the number on screen may be a roll no single mod "
    "made"
)
"""Why a tier was withheld from a line that reads like one affix.

Named so the panel, the CLI and a test can agree about which refusal happened without
matching on prose. See :meth:`ModDatabase._summed_sides`."""


def _as_summed(match: ModMatch) -> ModMatch:
    """Withdraw the tier claim from a line the game may have summed.

    The candidates stay: which *groups* can write this sentence here is still known,
    and :attr:`ModMatch.affix` is still unanimous, so the open-affix counts and the
    prefix/suffix label survive intact. Only the tier — the one number that would be
    read off a ladder the roll may never have been on — is withdrawn.
    """
    if not match.attribution.is_confident:
        return match
    return replace(match, attribution=Attribution.AMBIGUOUS, ceiling=None, note=SUMMED_NOTE)


def _with_note(match: ModMatch, note: str) -> ModMatch:
    return replace(match, note=note)


def _parse_time(value: Any) -> datetime:
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.fromtimestamp(0, tz=UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.fromtimestamp(0, tz=UTC)


def load(path: Path | None = None) -> ModDatabase:
    """Read the committed artifact. The only entry point; nothing else opens the file."""
    target = path or DATA_FILE
    try:
        document = json.loads(target.read_text("utf-8"))
    except FileNotFoundError as exc:
        raise ModDbUnavailable(
            f"no mod database at {target}. It is a committed build artifact — run "
            "'python scripts/build_moddb.py' to produce it."
        ) from exc
    except (OSError, ValueError) as exc:
        raise ModDbUnavailable(f"the mod database at {target} could not be read: {exc}") from exc

    try:
        return ModDatabase(document)
    except ModDbUnavailable:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ModDbUnavailable(f"the mod database at {target} is malformed: {exc!r}") from exc
