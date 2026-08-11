"""Raw GGG JSON → the normalized model of SPEC §4.5.

Everything about the wire format is confined to this file. Three of its decisions
are not obvious and are worth reading before changing anything:

**1. Category comes from the icon, not from the base type.**
The character and stash endpoints do not return the ``extended.category`` field that
the trade API does, so the category has to be derived. Base-type string matching is
the obvious approach and it is wrong: base types are localized, they change between
leagues, and "Two-Stone Ring" versus "Sanctified Mana Flask" is a taxonomy nobody
maintains. The ``icon`` URL, however, embeds the *art path* — ``2DItems/Rings/…``,
``2DItems/Flasks/…``, ``2DItems/Currency/…`` — which is language-independent, stable
across leagues, and assigned by GGG. It is the best signal available here. Base-type
matching survives only as a last resort.

**2. ``<<set:…>>`` tokens are stripped.**
Non-English clients prefix ``name`` and ``typeLine`` with gender/plurality markers,
exactly as they do in ``Client.txt`` (SPEC §4.6). An unstripped name will not match
any price table.

**3. Mods are accepted as strings or as objects.**
GGG's own endpoints return ``["+59 to Armour"]``. Some intermediaries — including
the MCP server used to record the fixtures — reshape them into
``[{"description": …}]``. Accepting both costs three lines and removes a whole class
of "works against the fixture, fails against the API" bug.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from modules.poeapi.backend.models import (
    Gem,
    Grid,
    Location,
    Mods,
    NormalizedItem,
    Rarity,
    Sockets,
    Source,
)

__all__ = [
    "category_of",
    "gem_of",
    "icon_art_path",
    "map_tier_of",
    "normalize_item",
    "normalize_items",
    "rarity_of",
    "strip_set_tokens",
]

# `<<set:MS>><<set:M>><<set:S>>Corruption Girdle` → `Corruption Girdle`
_SET_TOKEN = re.compile(r"<<[^>]*>>")

_GEN_IMAGE = re.compile(r"/gen/image/([A-Za-z0-9_\-=]+)")

BAG_SLOT = "MainInventory"

# frameType, straight out of GGG's item schema.
_FRAME_RARITY: dict[int, Rarity] = {
    0: Rarity.NORMAL,
    1: Rarity.MAGIC,
    2: Rarity.RARE,
    3: Rarity.UNIQUE,
    4: Rarity.GEM,
    5: Rarity.CURRENCY,
    6: Rarity.DIVINATION,
    7: Rarity.QUEST,
    8: Rarity.PROPHECY,
    9: Rarity.RELIC,
}

# frameTypes that *are* the category. A gem is a gem regardless of its art path.
_FRAME_CATEGORY: dict[int, tuple[str, str | None]] = {
    4: ("gem", None),
    5: ("currency", None),
    6: ("card", None),
    7: ("quest", None),
    8: ("prophecy", None),
}

# Art-path prefix → (category, subcategory). Longest prefix wins, so
# `2DItems/Armours/Shields` beats `2DItems/Armours`.
_ART_CATEGORY: dict[str, tuple[str, str | None]] = {
    "2DItems/Amulets/Talismans": ("accessory", "talisman"),
    "2DItems/Amulets": ("accessory", "amulet"),
    "2DItems/Rings": ("accessory", "ring"),
    "2DItems/Belts": ("accessory", "belt"),
    "2DItems/Armours/BodyArmours": ("armour", "body_armour"),
    "2DItems/Armours/Boots": ("armour", "boots"),
    "2DItems/Armours/Gloves": ("armour", "gloves"),
    "2DItems/Armours/Helmets": ("armour", "helmet"),
    "2DItems/Armours/Shields": ("armour", "shield"),
    "2DItems/Armours/Quivers": ("armour", "quiver"),
    "2DItems/Armours": ("armour", None),
    "2DItems/Weapons": ("weapon", None),
    "2DItems/Flasks": ("flask", None),
    "2DItems/Tinctures": ("tincture", None),
    "2DItems/Jewels": ("jewel", None),
    "2DItems/Currency": ("currency", None),
    "2DItems/Divination": ("card", None),
    "2DItems/Gems": ("gem", None),
    "2DItems/Quest": ("quest", None),
    "2DItems/Relics": ("relic", None),
    "2DItems/Maps": ("map", None),
    "2DItems/Hideout": ("hideout", None),
    "2DItems/MicrotransactionItemEffects": ("cosmetic", None),
}

# Weapon art lives one level down: `2DItems/Weapons/OneHandWeapons/Daggers/…`.
_WEAPON_SUB = re.compile(r"2DItems/Weapons/(?:OneHandWeapons|TwoHandWeapons)/([A-Za-z]+)")

_SLOT_CATEGORY: dict[str, tuple[str, str | None]] = {
    "Weapon": ("weapon", None),
    "Weapon2": ("weapon", None),
    "Offhand": ("armour", "shield"),
    "Offhand2": ("armour", "shield"),
    "Helm": ("armour", "helmet"),
    "BodyArmour": ("armour", "body_armour"),
    "Gloves": ("armour", "gloves"),
    "Boots": ("armour", "boots"),
    "Ring": ("accessory", "ring"),
    "Ring2": ("accessory", "ring"),
    "Amulet": ("accessory", "amulet"),
    "Belt": ("accessory", "belt"),
    "Flask": ("flask", None),
    "Trinket": ("accessory", "trinket"),
}

_BASE_SUFFIX_CATEGORY: tuple[tuple[str, tuple[str, str | None]], ...] = (
    ("Flask", ("flask", None)),
    ("Jewel", ("jewel", None)),
    ("Ring", ("accessory", "ring")),
    ("Amulet", ("accessory", "amulet")),
    ("Belt", ("accessory", "belt")),
    ("Talisman", ("accessory", "talisman")),
    ("Map", ("map", None)),
    ("Quiver", ("armour", "quiver")),
)

MAP_TIER_PROPERTY = "Map Tier"
STACK_SIZE_PROPERTY = "Stack Size"
GEM_LEVEL_PROPERTY = "Level"
GEM_QUALITY_PROPERTY = "Quality"

_LEADING_NUMBER = re.compile(r"-?\d+")
"""``"20 (Max)"`` → ``20``; ``"+20%"`` → ``20``.

Both gem properties arrive decorated. GGG appends ``(Max)`` to a gem at its level cap
and writes quality with a sign and a percent sign; both are display text, and the
number in front of them is what poe.ninja keys its variant on."""


def strip_set_tokens(text: str | None) -> str:
    """Remove localization markers and surrounding whitespace."""
    if not text:
        return ""
    return _SET_TOKEN.sub("", text).strip()


def icon_art_path(icon: str | None) -> str | None:
    """The ``2DItems/…`` art path encoded in a CDN icon URL, or ``None``.

    Modern URLs look like ``…/gen/image/<base64url>/<hash>/Belt4.png`` where the
    base64 segment decodes to ``[25, 14, {"f": "2DItems/Belts/Belt4", …}]``. Older
    ones embed the path literally. Both are handled; anything else yields ``None``
    and the caller falls through to the next signal.
    """
    if not icon:
        return None
    if "2DItems/" in icon:
        tail = icon[icon.index("2DItems/") :]
        return tail.split("?", 1)[0].rsplit(".", 1)[0]
    match = _GEN_IMAGE.search(icon)
    if not match:
        return None
    token = match.group(1)
    try:
        decoded = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        payload = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, list):
        return None
    for element in reversed(payload):
        if isinstance(element, Mapping):
            art = element.get("f")
            if isinstance(art, str) and art:
                return art
    return None


def rarity_of(raw: Mapping[str, Any]) -> Rarity:
    frame = raw.get("frameType")
    if isinstance(frame, bool) or not isinstance(frame, int):
        return Rarity.UNKNOWN
    return _FRAME_RARITY.get(frame, Rarity.UNKNOWN)


def _properties(raw: Mapping[str, Any]) -> dict[str, list[str]]:
    """``[{"name": "Map Tier", "values": [["14", 0]]}]`` → ``{"Map Tier": ["14"]}``."""
    out: dict[str, list[str]] = {}
    for group in ("properties", "additionalProperties", "notableProperties"):
        entries = raw.get(group)
        if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            name = strip_set_tokens(entry.get("name"))
            if not name:
                continue
            values: list[str] = []
            raw_values = entry.get("values")
            if isinstance(raw_values, Sequence) and not isinstance(raw_values, (str, bytes)):
                for pair in raw_values:
                    if isinstance(pair, Sequence) and not isinstance(pair, (str, bytes)) and pair:
                        values.append(str(pair[0]))
            out[name] = values
    return out


def category_of(
    raw: Mapping[str, Any],
    properties: Mapping[str, list[str]] | None = None,
) -> tuple[str, str | None]:
    """``(category, subcategory)``. See the module docstring for why, in this order.

    Returns ``("unknown", None)`` rather than guessing. An honest ``unknown`` is
    cheap to spot in the appraisal output; a confidently wrong category silently
    misprices an item, which is the failure mode this project cannot afford.
    """
    props = properties if properties is not None else _properties(raw)

    # 1. `extended.category`, when an endpoint bothers to send it.
    extended = raw.get("extended")
    if isinstance(extended, Mapping):
        category = extended.get("category")
        if isinstance(category, str) and category:
            subs = extended.get("subcategories")
            sub = None
            if isinstance(subs, Sequence) and subs and isinstance(subs[0], str):
                sub = subs[0].lower()
            return category.lower(), sub

    # 2. frameTypes that are categories in their own right.
    frame = raw.get("frameType")
    if isinstance(frame, int) and not isinstance(frame, bool) and frame in _FRAME_CATEGORY:
        return _FRAME_CATEGORY[frame]

    # 3. The art path.
    art = icon_art_path(raw.get("icon") if isinstance(raw.get("icon"), str) else None)
    if art:
        for prefix in sorted(_ART_CATEGORY, key=len, reverse=True):
            if art.startswith(prefix):
                category, sub = _ART_CATEGORY[prefix]
                if category == "weapon":
                    match = _WEAPON_SUB.match(art)
                    if match:
                        sub = match.group(1).lower()
                if category == "map":
                    # Fragments share the Maps art tree with maps. Only a map has a
                    # tier; that is the distinction the pricing tables draw too.
                    return ("map", sub) if MAP_TIER_PROPERTY in props else ("fragment", None)
                return category, sub

    # 4. The equipment slot it came out of.
    slot = raw.get("inventoryId")
    if isinstance(slot, str) and slot in _SLOT_CATEGORY:
        return _SLOT_CATEGORY[slot]

    # 5. Base-type suffix, the last resort.
    base = strip_set_tokens(raw.get("baseType")) or strip_set_tokens(raw.get("typeLine"))
    for suffix, result in _BASE_SUFFIX_CATEGORY:
        if base.endswith(suffix):
            return result
    if MAP_TIER_PROPERTY in props:
        return "map", None
    return "unknown", None


def _mod_strings(value: Any) -> list[str]:
    """A mod array, whether its entries are strings or ``{"description": …}``."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            text = strip_set_tokens(entry)
        elif isinstance(entry, Mapping):
            text = strip_set_tokens(entry.get("description") or entry.get("name"))
        else:
            continue
        if text:
            out.append(text)
    return out


def mods_of(raw: Mapping[str, Any]) -> Mods:
    return Mods(
        implicit=_mod_strings(raw.get("implicitMods")),
        explicit=_mod_strings(raw.get("explicitMods")),
        crafted=_mod_strings(raw.get("craftedMods")),
        enchant=_mod_strings(raw.get("enchantMods")),
        fractured=_mod_strings(raw.get("fracturedMods")),
        utility=_mod_strings(raw.get("utilityMods")),
        veiled=_mod_strings(raw.get("veiledMods")),
    )


def sockets_of(raw: Mapping[str, Any]) -> Sockets:
    """Socket count, largest link group, and colours in the API's order."""
    entries = raw.get("sockets")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return Sockets()
    colors: list[str] = []
    groups: dict[Any, int] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        colour = entry.get("sColour") or entry.get("attr") or "?"
        colors.append(str(colour))
        group = entry.get("group")
        groups[group] = groups.get(group, 0) + 1
    return Sockets(
        count=len(colors),
        links=max(groups.values()) if groups else 0,
        colors=colors,
    )


def influences_of(raw: Mapping[str, Any]) -> list[str]:
    """``{"shaper": true, "elder": false}`` → ``["shaper"]``, sorted for stability."""
    found: set[str] = set()
    block = raw.get("influences")
    if isinstance(block, Mapping):
        found.update(str(key) for key, value in block.items() if value)
    # Pre-3.9 responses carried these as top-level booleans.
    for legacy in ("shaper", "elder"):
        if raw.get(legacy) is True:
            found.add(legacy)
    return sorted(found)


def stack_size_of(
    raw: Mapping[str, Any],
    properties: Mapping[str, list[str]],
) -> tuple[int, int | None]:
    """``(stack_size, max_stack_size)``.

    Note that ``stackSize`` legitimately exceeds ``maxStackSize`` in special tabs
    (research-notes §7: ``Vaal Orb 163/20``), so nothing here clamps one to the other.
    """
    size = raw.get("stackSize")
    maximum = raw.get("maxStackSize")
    if not isinstance(size, int) or isinstance(size, bool):
        size = None
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        maximum = None
    if size is None:
        # Some responses only carry it as the "Stack Size" property, "290/5000".
        values = properties.get(STACK_SIZE_PROPERTY)
        if values:
            text = values[0].replace(",", "")
            head, _, tail = text.partition("/")
            if head.strip().isdigit():
                size = int(head.strip())
            if maximum is None and tail.strip().isdigit():
                maximum = int(tail.strip())
    return (size if size and size > 0 else 1), maximum


def map_tier_of(properties: Mapping[str, list[str]]) -> int | None:
    """The ``Map Tier`` property as an int, or ``None`` when the item is not a map."""
    values = properties.get(MAP_TIER_PROPERTY)
    if not values:
        return None
    text = values[0].strip()
    return int(text) if text.isdigit() else None


def _property_number(properties: Mapping[str, list[str]], name: str) -> int | None:
    values = properties.get(name)
    if not values:
        return None
    match = _LEADING_NUMBER.search(values[0])
    return int(match.group(0)) if match else None


def gem_of(category: str, properties: Mapping[str, list[str]]) -> Gem | None:
    """A gem's level and quality, or ``None`` for anything that is not a gem.

    Quality missing is quality zero — GGG omits the property rather than sending
    ``+0%``, and every uncorrupted gem that drops is 0%. Level missing is *unknown*,
    and stays unknown: it is the axis worth three orders of magnitude, so the one
    thing that must not happen is a default.
    """
    if category != "gem":
        return None
    return Gem(
        level=_property_number(properties, GEM_LEVEL_PROPERTY),
        quality=_property_number(properties, GEM_QUALITY_PROPERTY) or 0,
    )


def _uid(raw: Mapping[str, Any], location: Location) -> str:
    """GGG's item id, or a deterministic stand-in built from where it sits.

    Every item the endpoints return has an ``id`` today. The fallback exists because
    a missing uid would silently collapse two items into one in every hash, set and
    dict downstream, and that is not a failure worth discovering in the field.
    """
    item_id = raw.get("id")
    if isinstance(item_id, str) and item_id:
        return item_id
    seed = json.dumps(
        [
            strip_set_tokens(raw.get("typeLine")),
            strip_set_tokens(raw.get("name")),
            raw.get("x"),
            raw.get("y"),
            location.source.value,
            location.slot,
            location.tab_id,
            location.tab_index,
        ],
        sort_keys=True,
    )
    return "synthetic-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()


def normalize_item(raw: Mapping[str, Any], location: Location) -> NormalizedItem:
    """One raw item object → one :class:`NormalizedItem`."""
    props = _properties(raw)
    category, subcategory = category_of(raw, props)
    stack, max_stack = stack_size_of(raw, props)
    name = strip_set_tokens(raw.get("name"))
    type_line = strip_set_tokens(raw.get("typeLine"))
    base_type = strip_set_tokens(raw.get("baseType")) or type_line

    note = raw.get("note")
    if not isinstance(note, str) or not note.strip():
        forum = raw.get("forum_note")
        note = forum if isinstance(forum, str) and forum.strip() else None

    return NormalizedItem(
        uid=_uid(raw, location),
        name=name or type_line or base_type,
        base_type=base_type,
        category=category,
        subcategory=subcategory,
        rarity=rarity_of(raw),
        ilvl=_int(raw.get("ilvl")),
        stack_size=stack,
        max_stack_size=max_stack,
        map_tier=map_tier_of(props),
        gem=gem_of(category, props),
        grid=Grid(
            x=_int(raw.get("x")),
            y=_int(raw.get("y")),
            w=_int(raw.get("w"), 1),
            h=_int(raw.get("h"), 1),
        ),
        sockets=sockets_of(raw),
        corrupted=bool(raw.get("corrupted", False)),
        fractured=bool(raw.get("fractured", False)),
        synthesised=bool(raw.get("synthesised", False)),
        identified=bool(raw.get("identified", True)),
        influences=influences_of(raw),
        mods=mods_of(raw),
        note=note.strip() if isinstance(note, str) else None,
        location=location,
        icon=raw.get("icon") if isinstance(raw.get("icon"), str) else None,
    )


def normalize_items(
    raw_items: Iterable[Any],
    *,
    source: Source,
    tab_id: str | None = None,
    tab_name: str | None = None,
    tab_index: int | None = None,
    split_equipment: bool = False,
) -> list[NormalizedItem]:
    """Normalize a list of raw items.

    ``split_equipment`` is for ``get-items``, whose single ``items`` array mixes the
    backpack with worn gear. With it on, ``inventoryId == "MainInventory"`` becomes
    :attr:`Source.BAG` and everything else :attr:`Source.EQUIPMENT`, so the bag total
    is not silently inflated by the gear the player is wearing.

    Socketed gems are **not** flattened into the list. They are returned by the API
    nested inside their host item, they occupy no bag slot, and hoisting them would
    make the item count disagree with what the player sees on screen.
    """
    out: list[NormalizedItem] = []
    for entry in raw_items or []:
        if not isinstance(entry, Mapping):
            continue
        slot = entry.get("inventoryId")
        slot = slot if isinstance(slot, str) else None
        item_source = source
        if split_equipment:
            item_source = Source.BAG if slot == BAG_SLOT else Source.EQUIPMENT
        out.append(
            normalize_item(
                entry,
                Location(
                    source=item_source,
                    tab_id=tab_id,
                    tab_name=tab_name,
                    tab_index=tab_index,
                    slot=slot,
                ),
            )
        )
    return out


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return int(value)
