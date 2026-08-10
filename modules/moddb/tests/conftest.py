"""Fixtures for the `moddb` tests.

Self-contained rather than reaching for ``tests/conftest.py``: that one builds an
HTTP stack this module has no use for, and a data module that needed a mocked network
to be tested would be telling you something.

The database is loaded **once** for the whole session. It is immutable and every test
only reads, and re-parsing half a megabyte of JSON per test would turn a fast suite
into a slow one for no gain.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from modules.moddb.backend.database import DATA_FILE, ModDatabase, load

REPO_ROOT = Path(__file__).resolve().parents[3]
POEAPI_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "poeapi"
PRICE_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "prices"
APPRAISAL_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "appraisal"


@pytest.fixture(scope="session")
def db() -> ModDatabase:
    return load()


@pytest.fixture(scope="session")
def artifact() -> dict[str, Any]:
    """The raw artifact, for the tests that are about the file rather than the API."""
    return json.loads(DATA_FILE.read_text("utf-8"))


@pytest.fixture
def live_items() -> list[dict[str, Any]]:
    """Every item from the two fixtures that were read off a live account.

    ``prices/bag.json`` and ``appraisal/loot-bag.json`` are deliberately excluded:
    both were written by hand to exercise other modules, both carry ``ilvl: 0`` or
    invented rolls, and a mod database judged against invented rolls is being judged
    against somebody's memory of the game.
    """
    items: list[dict[str, Any]] = []
    for name in ("get-items.json", "get-stash-items.json"):
        payload = json.loads((POEAPI_FIXTURES / name).read_text("utf-8"))
        items.extend(payload["items"])
    return items


@pytest.fixture
def trade_stats() -> Any:
    """The recorded ``/api/trade/data/stats`` response `prices` is built against."""
    return json.loads((PRICE_FIXTURES / "trade-stats.json").read_text("utf-8"))


@pytest.fixture
def written_artifact(tmp_path: Path, artifact: dict[str, Any]) -> Iterator[Path]:
    """A copy of the artifact a test may corrupt without touching the real one."""
    target = tmp_path / "moddb.json"
    target.write_text(json.dumps(artifact), "utf-8")
    yield target
