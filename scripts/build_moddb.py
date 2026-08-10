#!/usr/bin/env python3
"""Build ``modules/moddb/data/moddb.json`` from repoe-fork.

**This is a build step, never a runtime path.** It is the only thing in the project
allowed to fetch game data, and nothing it produces is downloaded by the app: the
artifact it writes is committed, because a Decky plugin has no pip and no permission
to reach the internet on the user's behalf for 30 MB of JSON.

## Why a trim step exists at all

Upstream is three files totalling ~30 MB:

| file | bytes |
|---|---|
| ``mods.min.json`` | ~22 MB |
| ``stat_translations.min.json`` | ~4.6 MB |
| ``base_items.min.json`` | ~2.9 MB |

Almost none of it is about rollable affixes. 26 000 of the 40 000 mods are unique-item
mods, monster mods, map mods, atlas mods, crucible trees and league mechanics; the
translation file exists to render stats the tool never displays. What the consumers
in ``modules/moddb/backend/api.py`` need is: which *rollable* mods exist, what text
each one produces, what range it can roll, which bases it can appear on, what level
it needs, and which influence pool it belongs to. That is a few hundred kilobytes.

## Regeneration — read this

**Re-run this every league, and after every patch that touches mods.** A stale mod
database does not fail loudly; it answers confidently and wrongly, which is the worst
failure this project has. GGG adds tiers, moves spawn weights and re-levels mods in
almost every patch, so a database from last league will say "T1" about a roll that is
now T3.

    ./.venv/bin/python scripts/build_moddb.py            # fetch and write the artifact
    ./.venv/bin/python scripts/build_moddb.py --check    # is the committed one current?
    ./.venv/bin/python scripts/build_moddb.py --source-dir /tmp/repoe   # offline rebuild

The artifact stamps ``source.game_version`` (upstream's ``version.txt``, e.g.
``3.29.2.1``) and ``source.generated_at``. Both are readable at runtime through
``ModDbApi.version()`` so a surface can say how old the answer is, and
``DbVersion.describe()`` is written to be shown to a player.

## The one non-obvious trick

``mods.json`` already carries a rendered ``text`` per mod — ``"+(85-99) to maximum
Life"`` — with every index handler already applied (``base_mana_regeneration_rate_
per_minute`` 300-420 really does arrive as ``"Regenerate (5-7) Mana per second"``).
So this script does **not** reimplement RePoE's translation renderer. It normalizes
that text to the ``#`` form the trade API uses and records the value slots, and
``stat_translations.json`` is then needed only for one thing: bridging our normalized
text to the trade API's opaque stat ids, which is how ``prices`` talks about mods.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from modules.moddb.backend.api import denominate

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT = REPO_ROOT / "modules" / "moddb" / "data" / "moddb.json"

SOURCE_URL = "https://repoe-fork.github.io/"
SOURCE_FILES = ("mods.min.json", "base_items.min.json", "stat_translations.min.json")
VERSION_FILE = "version.txt"

SCHEMA = 1

USER_AGENT = "poedex-build/0.1 (+https://github.com/; mod database trim step)"

# ---------------------------------------------------------------------------
# What survives the trim
# ---------------------------------------------------------------------------

AFFIX_DOMAINS: Mapping[str, str] = {
    # RePoE domain -> the origin a consumer will call it by. Everything else is
    # dropped: `area` is map mods, `monster` is rare-monster mods, `atlas`,
    # `watchstone`, `heist_*`, `sanctum_relic` and the rest are league furniture.
    "item": "item",
    "crafted": "crafted",
    "flask": "flask",
    "abyss_jewel": "abyss_jewel",
    "unveiled": "unveiled",
    "tincture": "tincture",
    # Ordinary jewels are `misc`, not `item`, and so are their bases. Missing that
    # is not a small gap: a Cobalt Jewel would answer "not a base that rolls
    # affixes", which reads like a bug in the item rather than in the database.
    "misc": "jewel",
    "affliction_jewel": "cluster_jewel",
}

AFFIX_TYPES = frozenset({"prefix", "suffix"})
"""The only two generation types that are affixes on a rare.

Deliberately excludes ``enchantment`` (helmet/boot lab enchants are not affixes and
do not occupy an affix slot), ``corrupted`` (implicit corruptions), ``unique``,
``crucible_tree``, the Eater/Exarch implicits, and everything scourge."""

BASE_DOMAINS = frozenset({"item", "flask", "abyss_jewel", "tincture", "misc", "affliction_jewel"})
"""Bases that can carry affixes. Drops currency, gems, maps, cards, quest items."""

TRADE_ORIGINS = ("explicit", "implicit", "crafted", "fractured", "enchant")
"""Which of GGG's stat namespaces are worth carrying.

``pseudo`` is dropped because it is an aggregate ("+#% total Elemental Resistance")
that no single mod produces, and ``prices`` already refuses it for the same reason.
``scourge`` is dropped because Scourge is over."""

INFLUENCES = ("shaper", "elder", "crusader", "hunter", "redeemer", "warlord")
"""Fixed order — a mod's influence pools are stored as a bitmask over this tuple."""

INFLUENCE_TAGS: Mapping[str, str] = {
    # RePoE spells the Conquerors by their internal names. The right-hand side is
    # what a player and the trade site call them, and what `NormalizedItem.influences`
    # already carries.
    "shaper": "shaper",
    "elder": "elder",
    "crusader": "crusader",
    "basilisk": "hunter",
    "eyrie": "redeemer",
    "adjudicator": "warlord",
}

# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------
#
# `denominate` is imported from the module rather than written twice. It is the
# single point where a database mod and a live item have to agree: the build step
# reduces "+(85-99) to maximum Life" to "+# to maximum Life", the runtime reduces
# "+95 to maximum Life" to the same string, and if those were ever two functions
# that drifted, every lookup in the game would miss and the module would report
# "unknown" for everything — a failure that looks like caution.


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def fetch(name: str) -> bytes:
    request = urllib.request.Request(SOURCE_URL + name, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def load_sources(source_dir: Path | None) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str]:
    """Return ``(documents, provenance, game_version)``."""
    documents: dict[str, Any] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for name in SOURCE_FILES:
        if source_dir is not None:
            raw = (source_dir / name).read_bytes()
        else:
            print(f"  fetching {SOURCE_URL}{name} ...", file=sys.stderr)
            raw = fetch(name)
        documents[name] = json.loads(raw)
        provenance[name] = {
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if source_dir is not None and (source_dir / VERSION_FILE).is_file():
        version = (source_dir / VERSION_FILE).read_text("utf-8").strip()
    elif source_dir is not None:
        version = "unknown"
    else:
        version = fetch(VERSION_FILE).decode("utf-8").strip()
    return documents, provenance, version


# ---------------------------------------------------------------------------
# The trade-id bridge
# ---------------------------------------------------------------------------


def trade_index(translations: Sequence[Mapping[str, Any]]) -> tuple[
    dict[str, dict[str, str]], dict[str, tuple[str, ...]]
]:
    """``normalized text -> {origin: trade stat id}`` and ``-> game stat ids``.

    ``stat_translations.json`` is the only document that carries both id spaces, and
    it carries them side by side: each entry names the game stat ids it renders and
    the trade stat ids GGG assigned to the rendered sentence. Both sides are pushed
    through :func:`normalize` so that a lookup by an item's own text can never miss
    on formatting.

    First entry wins on a collision, matching how ``prices`` builds its own stat
    index — GGG's ``pseudo`` aggregates come first in their document and would
    otherwise shadow the explicit mod an item really has.
    """
    trade: dict[str, dict[str, str]] = {}
    game: dict[str, tuple[str, ...]] = {}
    for entry in translations:
        ids = tuple(str(i) for i in entry.get("ids") or ())
        for stat in entry.get("trade_stats") or ():
            text, stat_id = stat.get("text"), stat.get("id")
            origin = str(stat.get("type") or "")
            if not isinstance(text, str) or not isinstance(stat_id, str):
                continue
            if origin not in TRADE_ORIGINS or not stat_id.startswith(f"{origin}."):
                continue
            key, _slots = denominate(text)
            if not key:
                continue
            # The ``<origin>.`` prefix is reconstructed at load time; storing it
            # 3 000 times is 56 KiB of the same five words.
            trade.setdefault(key, {}).setdefault(origin, stat_id.split(".", 1)[1])
            if ids:
                game.setdefault(key, ids)
    return trade, game


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


class Vocab:
    """Intern strings to indices. Half of the size win is this."""

    def __init__(self) -> None:
        self._items: list[str] = []
        self._index: dict[str, int] = {}

    def __call__(self, value: str) -> int:
        found = self._index.get(value)
        if found is None:
            found = len(self._items)
            self._index[value] = found
            self._items.append(value)
        return found

    def list(self) -> list[str]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)


def positive_tags(spawn_weights: Iterable[Mapping[str, Any]]) -> list[tuple[str, int]]:
    """``spawn_weights`` as ``(tag, 0|1)``, order preserved.

    Order is load-bearing and the reason the zero entries cannot be dropped: the
    game takes the **first** matching tag, so ``[{"shield": 0}, {"armour": 1000}]``
    means "every armour except shields". Keeping only the positive entries would
    turn that into "shields too", which is a wrong answer that looks like data.
    """
    return [(str(w["tag"]), 1 if int(w.get("weight", 0)) > 0 else 0) for w in spawn_weights]


def influence_mask(tags: Sequence[tuple[str, int]]) -> int:
    """Which influence pools a mod belongs to, from its positive spawn tags.

    Influence mods are spelled ``helmet_shaper``, ``ring_basilisk`` and so on — the
    pool is a property of the tag, so it is read rather than listed. A bitmask over
    :data:`INFLUENCES`, because "no influence" is the answer for 93% of mods and
    ``0`` is one byte where ``[]`` is two.
    """
    mask = 0
    for tag, weight in tags:
        if not weight:
            continue
        name = INFLUENCE_TAGS.get(tag.rsplit("_", 1)[-1])
        if name is not None:
            mask |= 1 << INFLUENCES.index(name)
    return mask


def build(documents: Mapping[str, Any], provenance: Mapping[str, Any], version: str) -> dict:
    mods_doc: Mapping[str, Any] = documents["mods.min.json"]
    bases_doc: Mapping[str, Any] = documents["base_items.min.json"]
    translations: Sequence[Mapping[str, Any]] = documents["stat_translations.min.json"]

    trade_by_text, game_by_text = trade_index(translations)

    tags = Vocab()
    groups = Vocab()
    texts = Vocab()
    domains = Vocab()
    classes = Vocab()

    mods: list[list[Any]] = []
    stats = {"textless": 0, "unbridged": 0, "lines": 0, "malformed_slots": 0}

    # Spawn-weight vectors are interned: 7 000 mods share 743 distinct vectors,
    # because every tier of a group rolls on exactly the same bases. Without this
    # they are 180 KiB of repetition, more than the mod records themselves.
    spawn_vectors = Vocab()

    for mod_id, mod in sorted(mods_doc.items()):
        if mod.get("generation_type") not in AFFIX_TYPES:
            continue
        domain = AFFIX_DOMAINS.get(str(mod.get("domain")))
        if domain is None:
            continue
        raw_text = str(mod.get("text") or "")
        # One flat list per mod: ``[text, lo, hi, lo, hi, text, lo, hi, ...]``. How
        # many value slots a line has is the number of ``#`` in its text, so the
        # arity does not have to be stored beside every single line.
        lines: list[float] = []
        for line in raw_text.split("\n"):
            key, slots = denominate(line)
            if not key:
                continue
            if key.count("#") != len(slots):  # pragma: no cover - upstream oddity
                stats["malformed_slots"] += 1
                continue
            lines.append(texts(key))
            for low, high in slots:
                # Negated stats arrive with the ends the other way round —
                # ``(44-40)% less Duration`` is upstream's rendering of a range that
                # was negated after formatting. Storing it verbatim makes every
                # such roll fall outside its own tier.
                lines.append(_number(min(low, high)))
                lines.append(_number(max(low, high)))
            stats["lines"] += 1
            if key not in trade_by_text:
                stats["unbridged"] += 1
        if not lines:
            # A mod whose stats render to nothing — hidden helper stats, mostly.
            # Kept out entirely: it can never be matched from an item's mod text,
            # and counting it as a possible attribution would only add fog.
            stats["textless"] += 1
            continue
        weights = positive_tags(mod.get("spawn_weights") or ())
        vector = spawn_vectors(
            json.dumps([tags(tag) * 2 + weight for tag, weight in weights], separators=(",", ":"))
        )
        mods.append(
            [
                groups(str((mod.get("groups") or [mod_id])[0])),
                0 if mod["generation_type"] == "prefix" else 1,
                int(mod.get("required_level") or 0),
                domains(domain),
                vector,
                influence_mask(weights),
                1 if mod.get("is_essence_only") else 0,
                lines,
            ]
        )

    # -- bases ---------------------------------------------------------------
    #
    # Base *names* are the key, because a name is what `NormalizedItem.base_type`
    # carries and there is no metadata id anywhere near the item endpoints. Names
    # are not unique upstream (a Royale "Onyx Amulet" exists alongside the real
    # one), so the released, sellable variant wins and the rest are dropped.

    bases: dict[str, list[Any]] = {}
    ranking: dict[str, tuple[int, int]] = {}
    for base in bases_doc.values():
        if str(base.get("domain")) not in BASE_DOMAINS:
            continue
        name = str(base.get("name") or "")
        if not name:
            continue
        base_tags = [str(t) for t in base.get("tags") or ()]
        score = (
            1 if base.get("release_state") == "released" else 0,
            0 if "not_for_sale" in base_tags else 1,
        )
        if name in ranking and ranking[name] >= score:
            continue
        ranking[name] = score
        bases[name] = [
            classes(str(base.get("item_class") or "")),
            sorted({tags(tag) for tag in base_tags}),
            int(base.get("drop_level") or 0),
            0,  # top affix level, filled in below
            # The base's own domain, and it is load-bearing rather than decoration.
            # An abyss-jewel prefix carries ``default`` at weight 1000 because its
            # domain already restricts it to abyss jewels; read the spawn weights
            # alone and every belt in the game can roll ``+250 to Armour``.
            domains(AFFIX_DOMAINS[str(base.get("domain"))]),
        ]

    # -- the ilvl question, answered from the data ---------------------------
    #
    # `gate.py` hardcodes "ilvl 86 matters on weapons, armour and accessories, except
    # talismans and trinkets". That is a guess about categories. The real question is
    # per base: what is the highest required_level of any affix that can roll here?
    # A Hubris Circlet's top prefix needs ilvl 86 and an Iron Hat's does not, and
    # nothing about the category says so.

    tag_names = tags.list()
    decoded = [
        [(tag_names[code >> 1], code & 1) for code in json.loads(vector)]
        for vector in spawn_vectors.list()
    ]
    natural: dict[int, list[tuple[int, list[tuple[str, int]]]]] = {}
    for mod in mods:
        # Essence-only and influence mods do not answer "does ilvl matter for this
        # base" — neither can appear on an ordinary drop no matter how high it rolls.
        if mod[5] or mod[6]:
            continue
        natural.setdefault(mod[3], []).append((mod[2], decoded[mod[4]]))
    cache: dict[tuple[int, tuple[int, ...]], int] = {}
    for record in bases.values():
        key = (record[4], tuple(record[1]))
        top = cache.get(key)
        if top is None:
            base_tag_names = {tag_names[i] for i in record[1]}
            top = 0
            for level, weights in natural.get(record[4], ()):
                if level > top and _spawns(weights, base_tag_names):
                    top = level
            cache[key] = top
        record[3] = top

    # -- text table ----------------------------------------------------------

    # ``text index -> [origin mask, id, ...]``. The ids are in :data:`TRADE_ORIGINS`
    # order for whichever bits are set, and collapse to a single entry when every
    # origin shares one number — which 89% of them do, because GGG numbers the
    # *sentence* and namespaces it afterwards.
    text_list = texts.list()
    trade_table: dict[str, list[Any]] = {}
    game_table: dict[str, list[str]] = {}
    for index, key in enumerate(text_list):
        bridged = trade_by_text.get(key)
        if bridged:
            mask = 0
            ids: list[str] = []
            for position, origin in enumerate(TRADE_ORIGINS):
                if origin in bridged:
                    mask |= 1 << position
                    ids.append(bridged[origin])
            trade_table[str(index)] = [mask, ids[0]] if len(set(ids)) == 1 else [mask, *ids]
        stat_ids = game_by_text.get(key)
        if stat_ids:
            game_table[str(index)] = list(stat_ids)

    artifact = {
        "schema": SCHEMA,
        "source": {
            "project": "repoe-fork",
            "url": SOURCE_URL,
            "game_version": version,
            "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
            "generator": "scripts/build_moddb.py",
            "files": dict(provenance),
        },
        "vocab": {
            "tags": tag_names,
            "groups": groups.list(),
            "texts": text_list,
            "domains": domains.list(),
            "classes": classes.list(),
            "influences": list(INFLUENCES),
            # The same six, spelled the way the spawn tags spell them. An influenced
            # item's extra tags are ``<class tag>_<internal name>`` — ``helmet_shaper``,
            # ``bow_basilisk`` — and the runtime has to be able to build those, or an
            # influence mod can never match anything: the *base* never carries them.
            "influence_tags": [
                next(internal for internal, name in INFLUENCE_TAGS.items() if name == wanted)
                for wanted in INFLUENCES
            ],
        },
        "spawn": [json.loads(vector) for vector in spawn_vectors.list()],
        "trade": trade_table,
        "game_stats": game_table,
        "mods": mods,
        "bases": bases,
    }
    artifact["counts"] = {
        "mods": len(mods),
        "bases": len(bases),
        "texts": len(text_list),
        "groups": len(groups),
        "spawn_vectors": len(spawn_vectors),
        "bridged_texts": len(trade_table),
    }
    artifact["_stats"] = stats
    return artifact


def _number(value: float) -> float | int:
    """``85.0`` is two characters longer than ``85`` and 24 000 of them are integral."""
    return int(value) if value == int(value) else value


def _spawns(weights: Sequence[tuple[str, int]], base_tags: set[str]) -> bool:
    """First matching tag wins — the game's own rule, restated in six lines."""
    for tag, weight in weights:
        if tag in base_tags:
            return bool(weight)
    return False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def render(artifact: Mapping[str, Any]) -> str:
    body = dict(artifact)
    body.pop("_stats", None)
    return json.dumps(body, separators=(",", ":"), sort_keys=False) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source-dir", type=Path, default=None,
                        help="read the three upstream files from here instead of fetching")
    parser.add_argument("--out", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true",
                        help="do not write; exit 1 if the committed artifact differs in content")
    args = parser.parse_args(argv)

    print("building the mod database", file=sys.stderr)
    documents, provenance, version = load_sources(args.source_dir)
    artifact = build(documents, provenance, version)

    counts = artifact["counts"]
    stats = artifact["_stats"]
    print(
        f"  game version {version}: {counts['mods']} affix mods, {counts['bases']} bases, "
        f"{counts['texts']} distinct mod texts, {counts['bridged_texts']} of them bridged "
        f"to a trade stat id ({stats['unbridged']} of {stats['lines']} mod lines unbridged, "
        f"{stats['textless']} textless mods dropped)",
        file=sys.stderr,
    )

    payload = render(artifact)
    if args.check:
        if not args.out.is_file():
            print(f"{args.out} does not exist", file=sys.stderr)
            return 1
        existing = json.loads(args.out.read_text("utf-8"))
        fresh = json.loads(payload)
        for document in (existing, fresh):
            document["source"] = {
                k: v for k, v in document["source"].items() if k != "generated_at"
            }
        if existing != fresh:
            print(
                "the committed mod database is out of date; re-run without --check",
                file=sys.stderr,
            )
            return 1
        print("the committed mod database matches upstream", file=sys.stderr)
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload, "utf-8")
    print(
        f"  wrote {args.out.relative_to(REPO_ROOT)} - {len(payload) / 1024:.0f} KiB",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
