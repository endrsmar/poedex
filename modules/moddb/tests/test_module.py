"""The module as the runtime sees it: contract, kind, lifecycle, methods.

Plus the two properties that are non-negotiable for a *core* module here: it depends
on nothing, and it cannot reach the network.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from modules.moddb.backend.api import ItemMods, ModDbApi, ModDbUnavailable, Origin
from modules.moddb.backend.database import SCHEMA
from modules.moddb.backend.module import STALE_AFTER_DAYS, ModDbModule
from runtime.context import ModuleContext
from runtime.events import EventBus
from runtime.methods import MethodRegistry
from runtime.module import validate_module
from runtime.registry import Registry
from runtime.settings import SettingsStore
from runtime.storage import StorageRoot

MODULE_DIR = Path(__file__).resolve().parents[1]

NETWORK_ROOTS = {
    "httpx",
    "http",
    "urllib",
    "socket",
    "requests",
    "aiohttp",
    "asyncio",
    "ssl",
    "ftplib",
    "smtplib",
    "telnetlib",
}


def test_the_module_satisfies_the_contract() -> None:
    module = ModDbModule()
    validate_module(module)
    assert (module.id, module.kind) == ("moddb", "core")
    assert module.requires == []
    assert module.provides is ModDbApi
    assert module.settings_schema() == {}


def test_no_file_in_the_module_can_reach_the_network() -> None:
    """A data module that could fetch would eventually fetch.

    The generic boundary tests cover cross-module imports; this covers the promise
    specific to this phase — the 30 MB of upstream JSON is a build-time concern and
    there is to be no runtime download path, not even an unused one.
    """
    offenders: list[str] = []
    for path in sorted(MODULE_DIR.rglob("*.py")):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.split(".", 1)[0] in NETWORK_ROOTS:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, offenders


def test_the_data_directory_holds_exactly_one_committed_artifact() -> None:
    files = sorted(p.name for p in (MODULE_DIR / "data").iterdir())
    assert files == ["moddb.json"], (
        "the upstream 30 MB sources must never be committed; only the trimmed artifact"
    )


def context(tmp_path: Path) -> ModuleContext:
    """A context with no resolver: `moddb` requires nothing, so nothing may be asked for."""

    def refuse(_module_id: str, api: type) -> object:  # pragma: no cover - never called
        raise AssertionError(f"moddb must not require anything, and asked for {api}")

    return ModuleContext(
        module_id="moddb",
        events=EventBus(),
        storage=StorageRoot(tmp_path).namespace("moddb"),
        settings=SettingsStore(tmp_path / "settings.json").view("moddb"),
        logger=logging.getLogger("test.moddb"),
        _resolve=refuse,
    )


async def test_start_loads_the_database_and_logs_its_age(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module = ModDbModule()
    with caplog.at_level(logging.INFO, logger="test.moddb"):
        await module.start(context(tmp_path))
    assert module.failure is None
    assert any("mod database for Path of Exile" in r.getMessage() for r in caplog.records)
    await module.stop()


async def test_a_missing_artifact_degrades_this_module_and_nothing_else(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """It should cost the tool its tiers, not its ability to read a bag."""
    module = ModDbModule(path=tmp_path / "absent.json")
    with caplog.at_level(logging.ERROR, logger="test.moddb"):
        await module.start(context(tmp_path))
    assert module.failure is not None
    assert "build_moddb.py" in module.failure
    with pytest.raises(ModDbUnavailable):
        module.version()


async def test_a_stale_database_warns_at_start(
    tmp_path: Path, artifact: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """The one thing a mod database must never do quietly is get old."""
    old = dict(artifact)
    old["source"] = {**artifact["source"], "generated_at": "2020-01-01T00:00:00+00:00"}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(old), "utf-8")
    module = ModDbModule(path=path)
    with caplog.at_level(logging.WARNING, logger="test.moddb"):
        await module.start(context(tmp_path))
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("answers confidently and wrongly" in w for w in warnings)
    assert module.version().age_days() > STALE_AFTER_DAYS


async def test_the_registry_resolves_it_without_any_dependency(tmp_path: Path) -> None:
    registry = Registry(
        events=EventBus(),
        storage=StorageRoot(tmp_path / "cache"),
        settings=SettingsStore(tmp_path / "settings.json"),
        methods=MethodRegistry(),
    )
    registry.register(ModDbModule())
    await registry.start_all()
    try:
        api = registry.api(ModDbApi)
        assert api.version().schema == SCHEMA
        assert api.base("Coral Ring").item_class == "Ring"
    finally:
        await registry.stop_all()


async def test_the_exposed_methods_return_json(tmp_path: Path) -> None:
    module = ModDbModule()
    await module.start(context(tmp_path))
    names = module.methods()
    assert set(names) == {"version", "base", "identify", "report"}
    assert all(not name.startswith("_") for name in names)

    version = await module.version_json()
    assert version["game_version"] == module.version().game_version

    base = await module.base_json("Hubris Circlet")
    assert base["is_top_tier"] is True
    assert await module.base_json("Chaos Orb") is None

    identified = await module.identify_json(
        "+95 to maximum Life", base_type="Eternal Burgonet", ilvl=86
    )
    assert identified["tier"] == 4
    assert json.loads(json.dumps(identified))["description"] == "T4 of 10"

    reported = await module.report_json(
        base_type="Eternal Burgonet",
        ilvl=86,
        explicit=["+95 to maximum Life", "+40% to Cold Resistance"],
        crafted=["+25% to Fire Resistance"],
    )
    assert reported["prefixes"] == 1
    assert reported["open_prefixes"] == 2
    await module.stop()


async def test_the_api_surface_is_reachable_through_the_protocol(tmp_path: Path) -> None:
    """Dependents get the Protocol, never the class — so it has to be complete."""
    module = ModDbModule()
    await module.start(context(tmp_path))
    api: ModDbApi = module
    assert api.identify("+95 to maximum Life", base_type="Coral Ring", ilvl=86).tier
    assert api.ceiling("+95 to maximum Life", base_type="Coral Ring") == 114.0
    assert api.trade_stat_id("+95 to maximum Life", origin=Origin.EXPLICIT)
    assert api.game_stat_ids("+95 to maximum Life")
    assert api.groups("Coral Ring")
    assert api.report(ItemMods(base_type="Coral Ring", ilvl=86, explicit=[])).base is not None
    await module.stop()
