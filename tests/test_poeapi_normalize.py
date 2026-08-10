"""Normalization: raw GGG JSON → the model of SPEC §4.5.

Every assertion runs against `tests/fixtures/poeapi/`, whose provenance and scrubbing
are documented in the README next to it. There is one test per category branch,
because a category silently coming out wrong is the failure mode that misprices an
item without ever looking broken.
"""

from __future__ import annotations

import json

import pytest

from modules.poeapi.backend.api import Location
from modules.poeapi.backend.models import Rarity, Source
from modules.poeapi.backend.module import _characters_from, _tabs_from
from modules.poeapi.backend.normalize import (
    category_of,
    icon_art_path,
    normalize_item,
    normalize_items,
    strip_set_tokens,
)
from tests.conftest import REPO_ROOT

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "poeapi"


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text("utf-8"))


@pytest.fixture(scope="module")
def raw_items() -> list[dict]:
    return fixture("get-items.json")["items"]


@pytest.fixture(scope="module")
def items(raw_items):
    return normalize_items(raw_items, source=Source.BAG, split_equipment=True)


def find(items, name_fragment: str):
    for item in items:
        if name_fragment.lower() in (item.name + " " + item.base_type).lower():
            return item
    raise AssertionError(f"no item matching {name_fragment!r} in the fixture")


# -- the fixture itself --------------------------------------------------------


def test_the_fixture_is_scrubbed():
    """Nothing identifying may be committed. The README says so; this enforces it."""
    text = "\n".join(
        (FIXTURES / name).read_text("utf-8")
        for name in (
            "get-characters.json",
            "get-items.json",
            "get-stash-tabs.json",
            "get-stash-items.json",
        )
    ).lower()
    for real in ("gladefall", "pallinadar", "retriator", "endrsmar", "maristgaming"):
        assert real not in text, f"{real!r} leaked into a fixture"


def test_every_fixture_item_normalizes(raw_items, items):
    assert len(items) == len(raw_items)
    assert all(item.uid for item in items)
    assert len({item.uid for item in items}) == len(items)


def test_normalized_items_are_json_serializable(items):
    """CLAUDE.md: everything crossing to the frontend is plain JSON."""
    for item in items:
        json.dumps(item.model_dump(mode="json"))


# -- the awkward strings -------------------------------------------------------


def test_localization_markers_are_stripped():
    assert strip_set_tokens("<<set:MS>><<set:M>><<set:S>>Onyx Amulet") == "Onyx Amulet"
    assert strip_set_tokens("") == ""
    assert strip_set_tokens(None) == ""


def test_a_localized_item_name_comes_out_clean(items):
    amulet = find(items, "Onyx Amulet")
    assert amulet.name == "Placeholder Pendant"
    assert amulet.base_type == "Onyx Amulet"
    assert "<<" not in amulet.name


def test_an_unnamed_item_falls_back_to_its_type_line(items):
    flask = find(items, "Basalt Flask")
    assert flask.name == "Bottomless Basalt Flask of the Walrus"
    assert flask.base_type == "Basalt Flask"


# -- categories, one test per branch -------------------------------------------


def test_icon_art_path_decodes_the_modern_form(raw_items):
    belt = next(i for i in raw_items if i.get("inventoryId") == "Belt")
    assert icon_art_path(belt["icon"]) == "2DItems/Belts/Belt4"


def test_icon_art_path_reads_the_legacy_literal_form():
    url = "https://web.poecdn.com/image/Art/2DItems/Currency/Delve/Resonator1.png?scale=1"
    assert icon_art_path(url) == "2DItems/Currency/Delve/Resonator1"


@pytest.mark.parametrize("bad", [None, "", "https://example.com/nope.png", "/gen/image/!!!!/x.png"])
def test_icon_art_path_gives_up_cleanly(bad):
    assert icon_art_path(bad) is None


@pytest.mark.parametrize(
    ("fragment", "category", "subcategory"),
    [
        ("Cloth Belt", "accessory", "belt"),
        ("Onyx Amulet", "accessory", "amulet"),
        ("Two-Stone Ring", "accessory", "ring"),
        ("Legion Plate", "armour", "body_armour"),
        ("Basalt Flask", "flask", None),
        ("Cobalt Jewel", "jewel", None),
        ("Chaos Orb", "currency", None),
        ("Placeholder Card", "card", None),
        ("Grotto Map", "map", None),
        ("Sacrifice at Dusk", "fragment", None),
        ("Added Fire Damage Support", "gem", None),
        ("Message in a Bottle", "quest", None),
        ("Spine Bow", "weapon", "bows"),
        ("Resonator", "currency", None),
    ],
)
def test_every_category_in_the_fixture(items, fragment, category, subcategory):
    item = find(items, fragment)
    assert item.category == category
    if subcategory is not None:
        assert item.subcategory == subcategory


def test_an_unrecognisable_item_is_unknown_rather_than_guessed(items):
    """An honest `unknown` is visible in the output; a wrong guess is not."""
    oddity = find(items, "Placeholder Oddity")
    assert oddity.category == "unknown"
    assert oddity.subcategory is None


def test_maps_and_fragments_share_art_and_are_split_by_tier(items):
    """Both live under `2DItems/Maps`; only a map has a `Map Tier` property."""
    assert find(items, "Grotto Map").category == "map"
    assert find(items, "Sacrifice at Dusk").category == "fragment"


def test_extended_category_wins_when_the_endpoint_sends_one():
    raw = {"frameType": 2, "baseType": "Foo", "extended": {"category": "Weapons",
                                                           "subcategories": ["Bow"]}}
    assert category_of(raw) == ("weapons", "bow")


def test_the_slot_is_used_when_there_is_no_usable_icon():
    raw = {"frameType": 2, "baseType": "Mystery", "inventoryId": "Helm"}
    assert category_of(raw) == ("armour", "helmet")


# -- rarity --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fragment", "rarity"),
    [
        ("Cloth Belt", Rarity.RARE),
        ("Basalt Flask", Rarity.MAGIC),
        ("Onyx Amulet", Rarity.UNIQUE),
        ("Chaos Orb", Rarity.CURRENCY),
        ("Placeholder Card", Rarity.DIVINATION),
        ("Added Fire Damage Support", Rarity.GEM),
        ("Message in a Bottle", Rarity.QUEST),
        ("Grotto Map", Rarity.NORMAL),
    ],
)
def test_rarity_is_a_faithful_reading_of_frame_type(items, fragment, rarity):
    assert find(items, fragment).rarity is rarity


def test_a_missing_frame_type_is_unknown_not_normal():
    item = normalize_item({"baseType": "X"}, Location(source=Source.BAG))
    assert item.rarity is Rarity.UNKNOWN


# -- mods ----------------------------------------------------------------------


def test_crafted_mods_are_kept_apart_from_explicit(items):
    belt = find(items, "Cloth Belt")
    assert belt.mods.explicit == [
        "+59 to Armour",
        "+144 to maximum Life",
        "+41% to Fire Resistance",
    ]
    assert belt.mods.crafted == ["+13% to Lightning and Chaos Resistances"]
    assert belt.mods.implicit == ["24% increased Stun and Block Recovery"]


def test_flask_utility_mods_survive(items):
    """Dropping them makes a good flask indistinguishable from a white one."""
    flask = find(items, "Basalt Flask")
    assert flask.mods.utility == ["20% more Armour"]


def test_enchant_fractured_and_veiled_mods_are_kept(items):
    assert find(items, "Onyx Amulet").mods.enchant == ["Allocates Constitution"]
    assert find(items, "Two-Stone Ring").mods.fractured == ["+65 to maximum Life"]
    assert find(items, "Spine Bow").mods.veiled == ["Veiled Suffix"]


def test_mods_are_accepted_as_objects_too():
    """The MCP recorder reshapes them; the API does not. Both must work."""
    raw = {
        "baseType": "Cloth Belt",
        "frameType": 2,
        "explicitMods": [{"description": "+59 to Armour"}, {"description": "+144 to Life"}],
    }
    item = normalize_item(raw, Location(source=Source.BAG))
    assert item.mods.explicit == ["+59 to Armour", "+144 to Life"]


# -- sockets, stacks, flags, grid ----------------------------------------------


def test_sockets_report_the_largest_link_group(items):
    chest = find(items, "Legion Plate")
    assert chest.sockets.count == 6
    assert chest.sockets.links == 5  # five in group 0, one in group 1
    assert chest.sockets.colors == ["R", "R", "G", "B", "R", "R"]


def test_a_two_group_weapon_reports_the_bigger_group(items):
    bow = find(items, "Spine Bow")
    assert bow.sockets.count == 3
    assert bow.sockets.links == 2


def test_an_abyss_or_resonator_socket_does_not_break_parsing(items):
    resonator = find(items, "Resonator")
    assert resonator.sockets.count == 1
    assert resonator.sockets.colors == ["DV"]


def test_stack_size_comes_from_the_field(items):
    assert find(items, "Chaos Orb").stack_size == 23
    assert find(items, "Chaos Orb").max_stack_size == 5000


def test_stack_size_falls_back_to_the_property():
    raw = {
        "baseType": "Chaos Orb",
        "frameType": 5,
        "properties": [{"name": "Stack Size", "values": [["290/5000", 0]], "displayMode": 0}],
    }
    item = normalize_item(raw, Location(source=Source.BAG))
    assert item.stack_size == 290
    assert item.max_stack_size == 5000


def test_an_unstacked_item_has_a_stack_size_of_one(items):
    assert find(items, "Legion Plate").stack_size == 1


def test_flags_are_read(items):
    amulet = find(items, "Onyx Amulet")
    assert amulet.corrupted is True
    assert amulet.synthesised is True
    assert amulet.identified is True

    ring = find(items, "Two-Stone Ring")
    assert ring.identified is False
    assert ring.fractured is True


def test_influences_are_sorted_names(items):
    assert find(items, "Onyx Amulet").influences == ["elder", "shaper"]
    assert find(items, "Legion Plate").influences == []


def test_grid_position_is_preserved(items):
    chest = find(items, "Legion Plate")
    assert (chest.grid.w, chest.grid.h) == (2, 3)
    card = find(items, "Placeholder Card")
    assert (card.grid.x, card.grid.y) == (1, 0)


def test_the_note_is_kept_verbatim(items):
    """Tier 0 of the pricing engine (SPEC §5.0). Phase 3 parses this."""
    assert find(items, "Onyx Amulet").note == "~price 3 divine"
    assert find(items, "Two-Stone Ring").note == "~b/o 25 chaos"
    assert find(items, "Chaos Orb").note is None


# -- identity and location -----------------------------------------------------


def test_the_bag_is_separated_from_worn_gear(items):
    bag = [i for i in items if i.location.source is Source.BAG]
    equipment = [i for i in items if i.location.source is Source.EQUIPMENT]
    assert len(equipment) == 3
    assert len(bag) == len(items) - 3
    assert all(i.location.slot == "MainInventory" for i in bag)


def test_socketed_gems_are_not_hoisted_into_the_bag(items, raw_items):
    """They occupy no bag slot; counting them would disagree with the screen."""
    names = {i.name for i in items}
    assert "Determination" not in names


def test_an_item_without_an_id_gets_a_stable_synthetic_uid(items):
    bottle = find(items, "Message in a Bottle")
    assert bottle.uid.startswith("synthetic-")
    again = normalize_items(
        fixture("get-items.json")["items"], source=Source.BAG, split_equipment=True
    )
    assert find(again, "Message in a Bottle").uid == bottle.uid


def test_stash_items_carry_their_tab(items):
    stash = normalize_items(
        fixture("get-stash-items.json")["items"],
        source=Source.STASH,
        tab_index=1,
        tab_name="Gear",
    )
    assert all(i.location.source is Source.STASH for i in stash)
    assert all(i.location.tab_index == 1 for i in stash)
    assert all(i.location.tab_name == "Gear" for i in stash)


def test_a_stack_size_above_the_maximum_is_not_clamped():
    """research-notes §7: `Vaal Orb 163/20` is legitimate in a currency tab."""
    stash = normalize_items(fixture("get-stash-items.json")["items"], source=Source.STASH)
    vaal = next(i for i in stash if i.base_type == "Vaal Orb")
    assert vaal.stack_size == 163
    assert vaal.max_stack_size == 20


# -- characters and tabs -------------------------------------------------------


def test_characters_parse():
    characters = _characters_from(fixture("get-characters.json"))
    assert [c.name for c in characters] == [
        "PlaceholderWarden",
        "PlaceholderHierophant",
        "PlaceholderJuggernaut",
    ]
    assert characters[0].current is True
    assert characters[0].class_name == "Warden"
    assert characters[0].level == 97


def test_stash_tabs_parse():
    tabs = _tabs_from(fixture("get-stash-tabs.json"))
    assert [t.name for t in tabs][:2] == ["C", "Gear"]
    assert tabs[0].type == "CurrencyStash"
    assert tabs[0].colour == "#c86432"
    assert tabs[4].hidden is True


def test_remove_only_tabs_are_flagged():
    """They can never gain items — the highest-leverage cache rule (notes §7)."""
    tabs = _tabs_from(fixture("get-stash-tabs.json"))
    assert [t.name for t in tabs if t.remove_only] == ["(Remove-only) Placeholder League"]


# -- hashing -------------------------------------------------------------------


def test_the_content_hash_ignores_item_order():
    from modules.poeapi.backend.models import ItemSet, Meta, utcnow

    raw = fixture("get-items.json")["items"]
    forward = normalize_items(raw, source=Source.BAG, split_equipment=True)
    backward = normalize_items(list(reversed(raw)), source=Source.BAG, split_equipment=True)
    meta = Meta(fetched_at=utcnow())
    a = ItemSet(items=forward, source=Source.BAG, meta=meta)
    b = ItemSet(items=backward, source=Source.BAG, meta=meta)
    assert a.content_hash == b.content_hash


def test_the_content_hash_moves_when_an_item_changes():
    from modules.poeapi.backend.models import ItemSet, Meta, utcnow

    raw = fixture("get-items.json")["items"]
    meta = Meta(fetched_at=utcnow())
    before = ItemSet(
        items=normalize_items(raw, source=Source.BAG, split_equipment=True),
        source=Source.BAG,
        meta=meta,
    )
    changed = json.loads(json.dumps(raw))
    next(i for i in changed if i.get("typeLine") == "Chaos Orb")["stackSize"] = 24
    after = ItemSet(
        items=normalize_items(changed, source=Source.BAG, split_equipment=True),
        source=Source.BAG,
        meta=meta,
    )
    assert before.content_hash != after.content_hash


def test_the_content_hash_ignores_a_rotating_icon_url():
    """CDN icon URLs carry a cache-busting segment; that is not an inventory change."""
    from modules.poeapi.backend.models import ItemSet, Meta, utcnow

    raw = fixture("get-items.json")["items"]
    meta = Meta(fetched_at=utcnow())
    moved = json.loads(json.dumps(raw))
    for entry in moved:
        if isinstance(entry.get("icon"), str):
            entry["icon"] = entry["icon"].replace("/0000000000/", "/9999999999/")
    a = ItemSet(
        items=normalize_items(raw, source=Source.BAG, split_equipment=True),
        source=Source.BAG,
        meta=meta,
    )
    b = ItemSet(
        items=normalize_items(moved, source=Source.BAG, split_equipment=True),
        source=Source.BAG,
        meta=meta,
    )
    assert a.content_hash == b.content_hash
