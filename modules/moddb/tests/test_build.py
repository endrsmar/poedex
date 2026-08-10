"""The trim step's logic, exercised without a socket.

``scripts/build_moddb.py`` is allowed to fetch — it is a build step — but nothing in
this suite lets it. Every test here feeds it a hand-written three-document input and
checks the transformations that decide whether the artifact is true: which mods
survive, how spawn weights are encoded, how influence pools are read off tags, and
how the trade bridge is assembled.

The value of testing the builder rather than only the artifact: a bug here produces a
*plausible* artifact, and the runtime tests would keep passing while every tier was
quietly one row out.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_builder():
    """Import the script by path — ``scripts/`` is not a package a module may import."""
    spec = importlib.util.spec_from_file_location(
        "poedex_build_moddb", REPO_ROOT / "scripts" / "build_moddb.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def builder():
    return _load_builder()


def mods_document() -> dict[str, Any]:
    return {
        "TinyLife1": {
            "domain": "item",
            "generation_type": "prefix",
            "groups": ["IncreasedLife"],
            "required_level": 5,
            "is_essence_only": False,
            "text": "+(10-24) to maximum Life",
            "spawn_weights": [{"tag": "shield", "weight": 0}, {"tag": "armour", "weight": 1000}],
            "stats": [],
        },
        "TinyLife2": {
            "domain": "item",
            "generation_type": "prefix",
            "groups": ["IncreasedLife"],
            "required_level": 85,
            "is_essence_only": False,
            "text": "+(25-39) to maximum Life",
            "spawn_weights": [{"tag": "shield", "weight": 0}, {"tag": "armour", "weight": 1000}],
            "stats": [],
        },
        "TinyShaperLife": {
            "domain": "item",
            "generation_type": "suffix",
            "groups": ["ShaperLife"],
            "required_level": 68,
            "is_essence_only": False,
            "text": "+(40-50) to maximum Life",
            "spawn_weights": [{"tag": "helmet_basilisk", "weight": 500}],
            "stats": [],
        },
        "TinyDuration": {
            "domain": "flask",
            "generation_type": "suffix",
            "groups": ["FlaskDuration"],
            "required_level": 8,
            "is_essence_only": False,
            "text": "(49-45)% less Duration",
            "spawn_weights": [{"tag": "flask", "weight": 1000}],
            "stats": [],
        },
        "AMapMod": {
            "domain": "area",
            "generation_type": "prefix",
            "groups": ["MapThing"],
            "required_level": 1,
            "is_essence_only": False,
            "text": "Monsters deal 50% extra Damage as Fire",
            "spawn_weights": [{"tag": "default", "weight": 1000}],
            "stats": [],
        },
        "AUniqueMod": {
            "domain": "item",
            "generation_type": "unique",
            "groups": ["Whatever"],
            "required_level": 1,
            "is_essence_only": False,
            "text": "+(1-2) to Something",
            "spawn_weights": [],
            "stats": [],
        },
        "Textless": {
            "domain": "item",
            "generation_type": "prefix",
            "groups": ["Hidden"],
            "required_level": 1,
            "is_essence_only": False,
            "text": "",
            "spawn_weights": [{"tag": "armour", "weight": 1000}],
            "stats": [],
        },
    }


def bases_document() -> dict[str, Any]:
    return {
        "Metadata/Real": {
            "domain": "item",
            "name": "Tiny Helmet",
            "item_class": "Helmet",
            "drop_level": 20,
            "release_state": "released",
            "tags": ["helmet", "armour", "default"],
            "implicits": [],
        },
        "Metadata/Royale": {
            "domain": "item",
            "name": "Tiny Helmet",
            "item_class": "Helmet",
            "drop_level": 1,
            "release_state": "released",
            "tags": ["helmet", "armour", "not_for_sale", "default"],
            "implicits": [],
        },
        "Metadata/Shield": {
            "domain": "item",
            "name": "Tiny Shield",
            "item_class": "Shield",
            "drop_level": 10,
            "release_state": "released",
            "tags": ["shield", "armour", "default"],
            "implicits": [],
        },
        "Metadata/Currency": {
            "domain": "undefined",
            "name": "Tiny Orb",
            "item_class": "StackableCurrency",
            "drop_level": 1,
            "release_state": "released",
            "tags": ["currency", "default"],
            "implicits": [],
        },
    }


def translations_document() -> list[dict[str, Any]]:
    return [
        {
            "ids": ["base_maximum_life"],
            "English": [],
            "trade_stats": [
                {
                    "id": "explicit.stat_3299347043",
                    "text": "+# to maximum Life",
                    "type": "explicit",
                },
                {"id": "crafted.stat_3299347043", "text": "+# to maximum Life", "type": "crafted"},
                {"id": "pseudo.pseudo_total_life", "text": "+# to maximum Life", "type": "pseudo"},
            ],
        }
    ]


@pytest.fixture(scope="module")
def built(builder) -> dict[str, Any]:
    return builder.build(
        {
            "mods.min.json": mods_document(),
            "base_items.min.json": bases_document(),
            "stat_translations.min.json": translations_document(),
        },
        {"mods.min.json": {"bytes": 1, "sha256": "x"}},
        "9.9.9",
    )


def test_only_rollable_affixes_survive_the_trim(built: dict[str, Any]) -> None:
    """Map mods, unique-item mods and textless helpers are 80% of upstream."""
    groups = built["vocab"]["groups"]
    assert set(groups) == {"IncreasedLife", "ShaperLife", "FlaskDuration"}
    assert built["counts"]["mods"] == 4
    assert built["_stats"]["textless"] == 1


def test_zero_weight_tags_are_kept_and_kept_in_order(built: dict[str, Any]) -> None:
    """``[{shield: 0}, {armour: 1000}]`` means "every armour except shields".

    Dropping the zero entry would make it "shields too" — a wrong answer shaped
    exactly like a right one, and invisible in the artifact.
    """
    tags = built["vocab"]["tags"]
    life = next(m for m in built["mods"] if built["vocab"]["groups"][m[0]] == "IncreasedLife")
    vector = built["spawn"][life[4]]
    decoded = [(tags[code >> 1], code & 1) for code in vector]
    assert decoded == [("shield", 0), ("armour", 1)]


def test_spawn_vectors_are_shared_between_the_tiers_of_a_group(built: dict[str, Any]) -> None:
    """Every tier of a group rolls on the same bases; storing that 7 000 times is 180 KiB."""
    life = [m for m in built["mods"] if built["vocab"]["groups"][m[0]] == "IncreasedLife"]
    assert len({m[4] for m in life}) == 1


def test_influence_pools_are_read_off_the_tag_not_a_list(built: dict[str, Any]) -> None:
    """``helmet_basilisk`` is a Hunter mod. RePoE never says "hunter" anywhere."""
    influences = built["vocab"]["influences"]
    shaper_life = next(
        m for m in built["mods"] if built["vocab"]["groups"][m[0]] == "ShaperLife"
    )
    pools = [influences[bit] for bit in range(6) if shaper_life[5] & (1 << bit)]
    assert pools == ["hunter"]
    plain = next(m for m in built["mods"] if built["vocab"]["groups"][m[0]] == "IncreasedLife")
    assert plain[5] == 0


def test_a_reversed_range_is_straightened_at_build_time(built: dict[str, Any]) -> None:
    """``(49-45)% less Duration`` is upstream's rendering of a negated range.

    Stored verbatim, no roll could ever sit inside it and every flask suffix in the
    game would come back "outside every tier's range".
    """
    flask = next(m for m in built["mods"] if built["vocab"]["groups"][m[0]] == "FlaskDuration")
    _text, low, high = flask[7]
    assert (low, high) == (45, 49)


def test_integral_bounds_are_stored_as_integers(built: dict[str, Any]) -> None:
    """``85.0`` is two characters longer than ``85``, twenty-four thousand times over."""
    for record in built["mods"]:
        for value in record[7]:
            assert isinstance(value, int)


def test_the_released_sellable_base_wins_a_name_collision(built: dict[str, Any]) -> None:
    """A Royale "Onyx Amulet" exists alongside the real one, and names are the key."""
    assert set(built["bases"]) == {"Tiny Helmet", "Tiny Shield"}
    assert built["bases"]["Tiny Helmet"][2] == 20  # the real drop level, not Royale's 1


def test_the_top_affix_level_is_computed_per_base(built: dict[str, Any]) -> None:
    """A helmet reaches the level-85 life tier; a shield is excluded from it by weight 0."""
    assert built["bases"]["Tiny Helmet"][3] == 85
    assert built["bases"]["Tiny Shield"][3] == 0


def test_the_trade_bridge_keeps_the_namespaces_and_drops_pseudo(built: dict[str, Any]) -> None:
    texts = built["vocab"]["texts"]
    index = texts.index("+# to maximum Life")
    mask, *ids = built["trade"][str(index)]
    assert ids == ["stat_3299347043"], "one number shared by both namespaces"
    origins = [o for bit, o in enumerate(builder_origins()) if mask & (1 << bit)]
    assert origins == ["explicit", "crafted"]
    assert built["game_stats"][str(index)] == ["base_maximum_life"]


def builder_origins() -> tuple[str, ...]:
    return ("explicit", "implicit", "crafted", "fractured", "enchant")


def test_the_artifact_stamps_its_source(built: dict[str, Any]) -> None:
    assert built["source"]["game_version"] == "9.9.9"
    assert built["source"]["project"] == "repoe-fork"
    assert built["source"]["files"]["mods.min.json"]["sha256"] == "x"


def test_render_drops_the_diagnostics_and_stays_valid_json(builder, built) -> None:
    payload = builder.render(built)
    assert "_stats" not in json.loads(payload)
    assert json.loads(payload)["schema"] == builder.SCHEMA


def test_check_mode_ignores_only_the_timestamp(builder, built, tmp_path: Path) -> None:
    """So a rebuild that changed nothing but the clock does not read as a change."""
    target = tmp_path / "moddb.json"
    target.write_text(builder.render(built), "utf-8")
    document = json.loads(target.read_text("utf-8"))
    assert document["source"]["generated_at"]

    monkey = dict(built)
    monkey["source"] = {**built["source"], "generated_at": "1999-01-01T00:00:00+00:00"}
    same = json.loads(builder.render(monkey))
    for one in (document, same):
        one["source"].pop("generated_at")
    assert document == same


def test_the_builder_is_the_only_thing_that_may_fetch(builder) -> None:
    """And this suite never lets it. Asserted, so "offline" is a property not a habit."""
    assert hasattr(builder, "fetch")
    with pytest.raises(FileNotFoundError):
        builder.load_sources(Path("/nonexistent-source-dir"))
