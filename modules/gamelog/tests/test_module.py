"""The module as the runtime sees it: contract, settings, events, API."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from modules.gamelog.backend.api import (
    GAMELOG_AVAILABLE,
    GAMELOG_UNAVAILABLE,
    ZONE_ENTERED,
    GameLogApi,
    LogState,
    ZoneKind,
)
from modules.gamelog.backend.locate import LogLocation, locate
from modules.gamelog.backend.module import GameLogModule
from modules.gamelog.backend.tailer import LogWatcher
from runtime.events import Event, EventBus
from runtime.methods import MethodRegistry
from runtime.module import validate_module
from runtime.registry import Registry
from runtime.settings import SettingsStore
from runtime.storage import StorageRoot

from .conftest import SteamTree, chat, entered, generating

Append = Callable[[Path, str], None]


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def collected(bus: EventBus) -> list[Event]:
    events: list[Event] = []
    bus.subscribe("gamelog.*", events.append)
    return events


@pytest.fixture
def registry(tmp_path: Path, bus: EventBus) -> Registry:
    return Registry(
        events=bus,
        storage=StorageRoot(tmp_path / "cache"),
        settings=SettingsStore(tmp_path / "config" / "settings.json"),
        methods=MethodRegistry(),
    )


async def _pump(module: GameLogModule, times: int = 2) -> None:
    """Drive the watcher by hand instead of waiting on its timer."""
    watcher = module._watcher
    assert watcher is not None
    for _ in range(times):
        await watcher.tick()


def _fixed(path: Path, *, exists: bool = True, origin: str = "library") -> LogWatcher:
    location = LogLocation(path=path, origin=origin, exists=exists)
    return LogWatcher(lambda: (location, [location]), on_lines=lambda lines: None)


def _nothing() -> LogWatcher:
    probed = [LogLocation(Path("/nowhere/steamapps/common/Path of Exile/logs/Client.txt"),
                          "library", False)]
    return LogWatcher(lambda: (None, probed), on_lines=lambda lines: None)


# -- the module contract -------------------------------------------------------------


def test_it_satisfies_the_module_contract():
    validate_module(GameLogModule())


def test_it_is_core_with_no_dependencies():
    """`gamelog` reads a file on disk. It has nothing to depend on, and a core
    module that grew a dependency on a feature would be a startup error."""
    module = GameLogModule()
    assert module.kind == "core"
    assert module.requires == []
    assert module.provides is GameLogApi


def test_the_declared_api_is_actually_implemented():
    assert isinstance(GameLogModule(), GameLogApi)


def test_the_settings_schema_is_valid(registry: Registry):
    module = GameLogModule()
    view = registry.settings.register(module.id, module.settings_schema())
    assert view.get("log_path") == ""
    assert view.get("poll_interval_seconds") == 1.0


async def test_it_starts_and_stops_cleanly_through_the_registry(registry: Registry, tmp_path: Path):
    module = GameLogModule(watcher=_fixed(tmp_path / "Client.txt", exists=False))
    registry.register(module)
    await registry.start_all()
    assert registry.is_started("gamelog")
    assert "gamelog.status" in registry.methods.names()
    await registry.stop_all()
    # A watcher task left running past stop() is a plugin that never unloads.
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "gamelog-watcher"]


# -- events --------------------------------------------------------------------------


async def test_a_zone_entry_is_published(
    registry: Registry, tmp_path: Path, collected: list[Event], append: Append
):
    log = tmp_path / "Client.txt"
    log.write_text("", encoding="utf-8")
    module = GameLogModule(watcher=_fixed(log))
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        append(log, generating("HideoutCanals", level=1) + "\n" + entered("Canal Hideout") + "\n")
        await _pump(module)
    finally:
        await registry.stop_all()

    zones = [e for e in collected if e.topic == ZONE_ENTERED]
    assert len(zones) == 1
    assert zones[0].source == "gamelog"
    assert zones[0].payload["kind"] == ZoneKind.HIDEOUT.value
    assert zones[0].payload["area_id"] == "HideoutCanals"
    assert zones[0].payload["name"] == "Canal Hideout"


async def test_a_spoofed_chat_line_publishes_nothing(
    registry: Registry, tmp_path: Path, collected: list[Event], append: Append
):
    log = tmp_path / "Client.txt"
    log.write_text("", encoding="utf-8")
    module = GameLogModule(watcher=_fixed(log))
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        append(log, chat("You have entered Hall of Grandmasters.") + "\n")
        await _pump(module, times=4)
    finally:
        await registry.stop_all()
    assert [e.topic for e in collected if e.topic == ZONE_ENTERED] == []


async def test_the_payload_is_json_serializable(
    registry: Registry, tmp_path: Path, collected: list[Event], append: Append
):
    """Everything crossing to a frontend is json.dumps'd (CLAUDE.md)."""
    import json

    log = tmp_path / "Client.txt"
    log.write_text("", encoding="utf-8")
    module = GameLogModule(watcher=_fixed(log))
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        append(log, entered("The Twilight Strand") + "\n")
        await _pump(module)
        status = await module.status_json()
    finally:
        await registry.stop_all()
    zones = [e for e in collected if e.topic == ZONE_ENTERED]
    assert json.loads(json.dumps(zones[0].payload))["name"] == "The Twilight Strand"
    assert json.loads(json.dumps(status))["state"] == LogState.WATCHING.value


async def test_an_unresolvable_log_says_so(registry: Registry, collected: list[Event]):
    """SPEC §4.6: degrade visibly, with the paths we tried."""
    module = GameLogModule(watcher=_nothing())
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        status = await module.status()
    finally:
        await registry.stop_all()

    unavailable = [e for e in collected if e.topic == GAMELOG_UNAVAILABLE]
    assert unavailable, "an unfindable log must never be silent"
    assert status.state is LogState.UNAVAILABLE
    assert not status.healthy
    assert status.searched, "the user needs to know where we looked"
    assert unavailable[0].payload["searched"] == list(status.searched)


async def test_recovery_is_announced_once(
    registry: Registry, tmp_path: Path, collected: list[Event]
):
    log = tmp_path / "Client.txt"
    module = GameLogModule(watcher=_fixed(log, exists=False, origin="override"))
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        assert [e.topic for e in collected] == [GAMELOG_UNAVAILABLE]
        log.write_text("", encoding="utf-8")
        await _pump(module, times=3)
    finally:
        await registry.stop_all()
    assert [e.topic for e in collected] == [GAMELOG_UNAVAILABLE, GAMELOG_AVAILABLE]


# -- the API surface -------------------------------------------------------------------


async def test_the_api_reports_path_state_and_last_event(
    registry: Registry, tmp_path: Path, append: Append
):
    log = tmp_path / "Client.txt"
    log.write_text("", encoding="utf-8")
    module = GameLogModule(watcher=_fixed(log))
    registry.register(module)
    await registry.start_all()
    try:
        api: GameLogApi = registry.api(GameLogApi)
        await _pump(module)
        assert await api.path() == str(log)
        assert (await api.status()).state is LogState.WATCHING
        assert await api.last_event() is None

        append(log, generating("MapWorldsGrotto", level=83) + "\n" + entered("Grotto") + "\n")
        await _pump(module)
        event = await api.last_event()
        assert event is not None
        assert (event.kind, event.area_id, event.level) == (ZoneKind.MAP, "MapWorldsGrotto", 83)
    finally:
        await registry.stop_all()


async def test_manual_re_resolve_picks_up_an_install_that_appeared(
    registry: Registry, steam: SteamTree, monkeypatch: pytest.MonkeyPatch
):
    """"I just moved the game to the SD card" must not mean "wait 30 seconds"."""
    module = GameLogModule()
    monkeypatch.delenv("POEDEX_GAMELOG_PATH", raising=False)
    monkeypatch.setattr(
        "modules.gamelog.backend.module.default_resolver",
        lambda **kwargs: (lambda: locate(roots=[steam.root])),
    )
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        assert (await module.status()).state is LogState.UNAVAILABLE
        log = steam.install()
        status = await module.resolve()
        assert status.state is LogState.WATCHING
        assert status.path == str(log)
    finally:
        await registry.stop_all()


async def test_status_before_start_is_stopped_not_a_crash():
    module = GameLogModule()
    status = await module.status()
    assert status.state is LogState.STOPPED
    assert await module.path() is None
    assert (await module.resolve()).state is LogState.STOPPED


async def test_the_registry_methods_return_plain_json(registry: Registry, tmp_path: Path):
    module = GameLogModule(watcher=_fixed(tmp_path / "Client.txt", exists=False))
    registry.register(module)
    await registry.start_all()
    try:
        result = await registry.methods.call("gamelog.status")
        assert isinstance(result, dict)
        assert result["state"] in {s.value for s in LogState}
    finally:
        await registry.stop_all()


# -- settings ---------------------------------------------------------------------------


async def test_the_log_path_setting_is_honoured(registry: Registry, tmp_path: Path):
    log = tmp_path / "manual" / "Client.txt"
    log.parent.mkdir()
    log.write_text("", encoding="utf-8")
    registry.settings.register("gamelog", GameLogModule().settings_schema())
    registry.settings.set("gamelog", "log_path", str(log))

    module = GameLogModule()
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        status = await module.status()
        assert status.path == str(log)
        assert status.origin == "override"
    finally:
        await registry.stop_all()


async def test_the_environment_override_beats_the_setting(
    registry: Registry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from modules.gamelog.backend.api import PATH_ENV

    chosen = tmp_path / "from-env" / "Client.txt"
    chosen.parent.mkdir()
    chosen.write_text("", encoding="utf-8")
    registry.settings.register("gamelog", GameLogModule().settings_schema())
    registry.settings.set("gamelog", "log_path", str(tmp_path / "from-settings" / "Client.txt"))
    monkeypatch.setenv(PATH_ENV, str(chosen))

    module = GameLogModule()
    registry.register(module)
    await registry.start_all()
    try:
        await _pump(module)
        assert (await module.status()).path == str(chosen)
    finally:
        await registry.stop_all()
