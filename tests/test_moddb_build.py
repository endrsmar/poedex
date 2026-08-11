"""``scripts/build_moddb.py`` must produce the same bytes from the same sources.

This is not a style preference. `modules/moddb/data/moddb.json` is committed, it has
to be regenerated every league — a stale mod database answers confidently and wrongly
about tiers GGG re-levelled — and a regeneration is only usable if its diff can be
*read*. Phase 9b declined to regenerate for exactly that reason: a rebuild differed
from the committed artifact and there was no way to tell an upstream change from
noise, so the fix it wanted to ship could not be shipped.

What was actually irreproducible is worth naming, because the suspected cause was
wrong. `locality_index` and `line_locality` were the obvious suspects — both handle
sets — and neither is order-sensitive: `line_locality` reduces its votes with
``all()``, and `_entry_keys`'s set only ever decided which *list* an entry was
appended to, never a position within one. Perturbing that order changes nothing, and
:func:`test_the_ordering_that_was_blamed_does_not_actually_matter` pins that so the
next person does not re-derive it.

The build was irreproducible in its **inputs and its stamp**, not its arithmetic:

* ``generated_at`` is a clock reading, so no two builds ever produced equal bytes and
  ``--check`` had to compare parsed JSON with one key deleted. ``SOURCE_DATE_EPOCH``
  now pins it.
* two of the four sources are fetched live, and one of them — GGG's
  ``/api/trade/data/stats`` — is not versioned, not immutable, and is the sole source
  of the local stat ids that decide every mod's locality flag. Its sha256 is recorded
  in the artifact, so a rebuild diff is now attributable to a named input.

So the proof here is the one that matters: **two builds, same bytes, different hash
seeds**, from a source sample committed beside this file. It runs offline; the fetch
path is not exercised and is asserted unreachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import build_moddb

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "tests" / "fixtures" / "moddb" / "source"
EPOCH = "1700000000"


def _load() -> tuple[dict, dict, str]:
    return build_moddb.load_sources(SOURCE)


@pytest.fixture(scope="module")
def sources() -> tuple[dict, dict, str]:
    return _load()


# -- the proof -----------------------------------------------------------------


def test_two_builds_from_the_same_sources_are_byte_identical(tmp_path: Path):
    """Two subprocesses, two hash seeds, one set of bytes.

    In-process would not be the same test. Python randomizes string hashing per
    *process*, so a single interpreter cannot disagree with itself about set order —
    which is precisely the class of bug this is meant to exclude. Two processes with
    deliberately different seeds can.
    """
    outputs = []
    for seed in ("0", "12345"):
        out = tmp_path / f"moddb-{seed}.json"
        environment = dict(os.environ, PYTHONHASHSEED=seed, SOURCE_DATE_EPOCH=EPOCH)
        result = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "build_moddb.py"),
                "--source-dir",
                str(SOURCE),
                "--out",
                str(out),
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(out.read_bytes())

    assert outputs[0] == outputs[1], (
        "the same four sources built two different artifacts; the build is not "
        "reproducible and a regeneration diff cannot be attributed"
    )
    assert len(outputs[0]) > 10_000, "an empty artifact is trivially reproducible"


def test_the_sample_exercises_the_parts_where_an_ordering_bug_could_hide(sources):
    """A determinism test over a sample with no hard cases proves nothing.

    Both readings of the ambiguous sentences have to be present — a sample that is
    all-local would pass with `line_locality` hardwired to ``True`` — as do the
    signed-key fallback, more than one domain, and the influence pools.
    """
    documents, provenance, version = sources
    artifact = build_moddb.build(documents, provenance, version)
    stats = artifact["_stats"]

    assert stats["ambiguous_lines"] > 20, stats
    assert 0 < stats["local_lines"] < stats["ambiguous_lines"], (
        "the sample answers the locality question only one way", stats
    )
    assert artifact["counts"]["local_texts"] > 5, artifact["counts"]

    assert len(artifact["vocab"]["domains"]) >= 4, artifact["vocab"]["domains"]
    assert any(mod[5] for mod in artifact["mods"]), "no influenced mod in the sample"
    assert any(mod[6] for mod in artifact["mods"]), "no essence-only mod in the sample"
    assert any("-#" in text for text in artifact["vocab"]["texts"]), "no signed sentence"
    assert version and version != "unknown"


def test_the_ordering_that_was_blamed_does_not_actually_matter(sources):
    """`_entry_keys`'s order was the suspect and is not the culprit — measured.

    Recorded because the next person to read the plan will read "almost certainly set
    iteration in ``locality_index`` / ``line_locality``" and start there. Feeding the
    build the same entries in reversed sentence order produces the same artifact,
    because every consumer of that order reduces over it rather than indexing into it.
    """
    documents, provenance, version = sources
    original = build_moddb._entry_keys
    try:
        build_moddb._entry_keys = lambda entry: tuple(reversed(original(entry)))
        reversed_build = build_moddb.render(build_moddb.build(documents, provenance, version))
    finally:
        build_moddb._entry_keys = original
    forward = build_moddb.render(build_moddb.build(documents, provenance, version))

    def undated(payload: str) -> dict:
        body = json.loads(payload)
        body["source"].pop("generated_at")
        return body

    assert undated(forward) == undated(reversed_build)


# -- the stamp, which was the only thing a rebuild could never reproduce ---------


def test_source_date_epoch_pins_the_one_field_a_clock_would_move(sources):
    documents, provenance, version = sources
    os.environ[build_moddb.SOURCE_DATE_EPOCH] = EPOCH
    try:
        first = build_moddb.build(documents, provenance, version)
        second = build_moddb.build(documents, provenance, version)
    finally:
        del os.environ[build_moddb.SOURCE_DATE_EPOCH]
    assert first["source"]["generated_at"] == "2023-11-14T22:13:20+00:00"
    assert build_moddb.render(first) == build_moddb.render(second)


def test_without_the_epoch_the_stamp_is_a_real_clock_reading(sources):
    """Pinning must not become the default: a regenerated artifact has to say when."""
    assert build_moddb.SOURCE_DATE_EPOCH not in os.environ
    documents, provenance, version = sources
    stamped = build_moddb.build(documents, provenance, version)["source"]["generated_at"]
    assert stamped != "2023-11-14T22:13:20+00:00"
    assert stamped.endswith("+00:00")


# -- what the artifact records, so a diff is attributable -----------------------


def test_every_source_is_recorded_by_hash(sources):
    """Four inputs, four sha256s. Two of them are fetched from endpoints that can
    change under you, and GGG's stats document is the sole origin of the local ids
    that set every mod's locality flag — so "did upstream move?" has to be answerable
    from the artifact alone rather than from a rebuild."""
    documents, provenance, version = sources
    files = build_moddb.build(documents, provenance, version)["source"]["files"]
    assert set(files) == {
        "mods.min.json",
        "base_items.min.json",
        "stat_translations.min.json",
        build_moddb.TRADE_STATS_FILE,
    }
    for name, record in files.items():
        assert len(record["sha256"]) == 64, name
        assert record["bytes"] > 0, name


def test_the_committed_artifact_is_within_the_size_guard():
    artifact = REPO_ROOT / "modules" / "moddb" / "data" / "moddb.json"
    size = artifact.stat().st_size
    assert size < 900 * 1024, f"{size / 1024:.0f} KiB"
    body = json.loads(artifact.read_text("utf-8"))
    assert body["schema"] == build_moddb.SCHEMA
    assert set(body["source"]["files"]) == {
        "mods.min.json",
        "base_items.min.json",
        "stat_translations.min.json",
        build_moddb.TRADE_STATS_FILE,
    }


# -- offline ---------------------------------------------------------------------


def test_the_build_under_test_never_reaches_the_network(monkeypatch, tmp_path: Path):
    """`--source-dir` is the whole offline contract, and it is worth an assertion
    rather than a reading: the script *is* allowed to fetch, so nothing else in the
    suite would notice if a source-dir build quietly did."""

    def forbidden(*args, **kwargs):  # pragma: no cover - the point is that it is not hit
        raise AssertionError("the build reached the network")

    monkeypatch.setattr(build_moddb, "fetch", forbidden)
    monkeypatch.setattr(build_moddb, "fetch_url", forbidden)
    monkeypatch.setenv(build_moddb.SOURCE_DATE_EPOCH, EPOCH)
    out = tmp_path / "moddb.json"
    assert build_moddb.main(["--source-dir", str(SOURCE), "--out", str(out)]) == 0
    assert out.is_file()


# -- the sample, and keeping it honest -------------------------------------------


def test_the_sample_is_self_consistent_and_small(sources):
    """It has to stay committable, and every part of it has to be reachable.

    A translation entry whose stats no sampled mod carries, or a base no sampled mod
    can roll on, is weight in the repository that changes no answer.
    """
    documents, _provenance, _version = sources
    assert sum(p.stat().st_size for p in SOURCE.iterdir()) < 512 * 1024

    mods = documents["mods.min.json"]
    stat_ids = {
        str(stat.get("id"))
        for mod in mods.values()
        for stat in mod.get("stats") or ()
        if stat.get("id")
    }
    for entry in documents["stat_translations.min.json"]:
        assert {str(i) for i in entry.get("ids") or ()} & stat_ids, entry.get("ids")

    assert 100 < len(mods) < 400
    assert all(mod.get("generation_type") in build_moddb.AFFIX_TYPES for mod in mods.values())


def test_the_sampler_is_itself_reproducible(sources, tmp_path: Path):
    """Otherwise the fixture is a one-off nobody can refresh.

    Next league somebody runs ``--sample-to`` against the new upstream and commits
    what comes out. If the sampler picked differently on identical input, that diff
    would be unreadable for exactly the reason the artifact's was — one level down,
    and in the file the proof rests on.
    """
    documents, _provenance, version = sources
    first, second = tmp_path / "a", tmp_path / "b"
    counts = build_moddb.sample(documents, version, first)
    assert build_moddb.sample(documents, version, second) == counts
    for path in sorted(first.iterdir()):
        assert (second / path.name).read_bytes() == path.read_bytes(), path.name
