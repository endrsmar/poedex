"""The committed artifact: its stamp, its size, and what happens when it is broken."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from modules.moddb.backend.api import ModDbUnavailable
from modules.moddb.backend.database import DATA_FILE, SCHEMA, load

MAX_BYTES = 900_000
"""What "a few hundred kilobytes" is allowed to mean.

The artifact is vendored into a Decky plugin's ``py_modules/`` and shipped in a
sideload zip, so its size is a product constraint rather than an aesthetic one. The
limit is deliberately close to the real figure — a doubling should have to be argued
for in a diff, not noticed a year later."""


def test_the_artifact_is_committed_and_small() -> None:
    assert DATA_FILE.is_file(), (
        f"{DATA_FILE} is missing. It is a committed build artifact — "
        "run 'python scripts/build_moddb.py'."
    )
    size = DATA_FILE.stat().st_size
    assert size < MAX_BYTES, f"the mod database has grown to {size / 1024:.0f} KiB"


def test_the_version_stamp_is_readable(db) -> None:
    """The whole point of the stamp: a surface can say how old the answer is.

    A mod database one league stale does not fail. It answers confidently and
    wrongly, and nothing on screen looks unusual — so the date has to be reachable
    from the API, not just present in the file.
    """
    version = db.version()
    assert version.schema == SCHEMA
    assert version.game_version.count(".") >= 2, version.game_version
    assert version.generated_at.tzinfo is not None
    assert version.generated_at.year >= 2024
    assert version.mods > 5000
    assert version.bases > 500
    assert "repoe-fork" in version.source


def test_describe_says_the_game_version_and_the_age(db) -> None:
    version = db.version()
    now = version.generated_at + timedelta(days=200)
    described = version.describe(now)
    assert version.game_version in described
    assert "200 days old" in described
    assert version.describe(version.generated_at).endswith("built today")
    assert version.age_days(now) == pytest.approx(200.0)
    assert version.age_days(version.generated_at - timedelta(days=5)) == 0.0


def test_the_stamp_survives_json_serialization(db) -> None:
    payload = db.version().to_json()
    assert json.loads(json.dumps(payload))["game_version"] == db.version().game_version
    assert payload["description"] == db.version().describe()


def test_provenance_records_what_it_was_built_from(artifact: dict[str, Any]) -> None:
    source = artifact["source"]
    assert source["project"] == "repoe-fork"
    assert source["generator"] == "scripts/build_moddb.py"
    assert set(source["files"]) == {
        "mods.min.json",
        "base_items.min.json",
        "stat_translations.min.json",
        # Phase 9b's fourth source. GGG's own filter list is the only published place
        # the local stat ids exist, and without it a body armour's `#% increased
        # Armour` goes out as the global id and matches nothing.
        "trade_stats.json",
    }
    for record in source["files"].values():
        # The digest is what makes "is this artifact current?" answerable without
        # re-running the whole build and diffing 500 KiB.
        assert len(record["sha256"]) == 64
        assert record["bytes"] > 1_000_000
    datetime.fromisoformat(source["generated_at"])


def test_a_missing_artifact_is_loud(tmp_path: Path) -> None:
    with pytest.raises(ModDbUnavailable, match=r"scripts/build_moddb\.py"):
        load(tmp_path / "nothing.json")


def test_an_unreadable_artifact_is_loud(tmp_path: Path) -> None:
    broken = tmp_path / "moddb.json"
    broken.write_text("{not json", "utf-8")
    with pytest.raises(ModDbUnavailable, match="could not be read"):
        load(broken)


def test_a_future_schema_is_refused_rather_than_guessed_at(written_artifact: Path) -> None:
    """An empty database answers "unknown" to everything, which looks like caution.

    That is why a schema this build does not understand raises instead of degrading:
    the two failure modes are indistinguishable on screen, and only one of them is
    honest.
    """
    document = json.loads(written_artifact.read_text("utf-8"))
    document["schema"] = SCHEMA + 1
    written_artifact.write_text(json.dumps(document), "utf-8")
    with pytest.raises(ModDbUnavailable, match=r"re-run scripts/build_moddb\.py"):
        load(written_artifact)


def test_a_truncated_artifact_is_loud(written_artifact: Path) -> None:
    document = json.loads(written_artifact.read_text("utf-8"))
    del document["vocab"]["texts"]
    written_artifact.write_text(json.dumps(document), "utf-8")
    with pytest.raises(ModDbUnavailable, match="malformed"):
        load(written_artifact)


def test_the_vocabularies_are_consistent(artifact: dict[str, Any]) -> None:
    """Every index in a mod record points at something that exists."""
    vocab = artifact["vocab"]
    tags, groups, texts = vocab["tags"], vocab["groups"], vocab["texts"]
    spawn, mods = artifact["spawn"], artifact["mods"]
    assert len(vocab["influences"]) == len(vocab["influence_tags"]) == 6
    for record in mods:
        group, affix, level, domain, vector, mask, essence, lines, local = record
        assert local >= 0
        assert 0 <= group < len(groups)
        assert affix in (0, 1)
        assert 0 <= level <= 100
        assert 0 <= domain < len(vocab["domains"])
        assert 0 <= vector < len(spawn)
        assert 0 <= mask < 1 << 6
        assert essence in (0, 1)
        assert lines, "a mod with no text can never be matched and should not be here"
    for vector in spawn:
        for code in vector:
            assert 0 <= code >> 1 < len(tags)
    for record in artifact["bases"].values():
        assert 0 <= record[0] < len(vocab["classes"])
        assert all(0 <= t < len(tags) for t in record[1])
        assert 0 <= record[4] < len(vocab["domains"])
    for index in artifact["trade"]:
        assert 0 <= int(index) < len(texts)


def test_generated_at_is_not_in_the_future(db) -> None:
    assert db.version().generated_at <= datetime.now(UTC) + timedelta(hours=1)
