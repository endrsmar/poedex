"""Shared fixtures.

Every test is offline and writes only inside ``tmp_path``. Nothing here touches the
real ``~/.config/poedex``; the ``poedex_home`` fixture is autouse so a test that
forgets to isolate itself still cannot.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from runtime.events import EventBus
from runtime.methods import MethodRegistry
from runtime.registry import Registry
from runtime.secrets import clear_secrets
from runtime.settings import SettingsStore
from runtime.storage import StorageRoot

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def poedex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path helper at a throwaway directory."""
    monkeypatch.setenv("POEDEX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("POEDEX_CACHE_DIR", str(tmp_path / "cache"))
    for leaked in ("DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(leaked, raising=False)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_secret_registry():
    """Secrets registered for redaction are process-wide; do not leak between tests."""
    clear_secrets()
    yield
    clear_secrets()


class FakeModule:
    """A module that records its lifecycle. Used to exercise the registry.

    This is the "trivial second module" of Phase 1's exit criteria — deliberately
    confined to the tests, since Phase 1's source tree ships exactly one module.
    """

    def __init__(
        self,
        module_id: str,
        *,
        kind: str = "feature",
        requires: list[str] | None = None,
        provides: type | None = None,
        wants: list[type] | None = None,
        methods: dict[str, Callable[..., Any]] | None = None,
        schema: dict[str, Any] | None = None,
        log: list[str] | None = None,
        fail_on_start: bool = False,
        fail_on_stop: bool = False,
    ) -> None:
        self.id = module_id
        self.name = module_id.title()
        self.kind = kind
        self.requires = list(requires or [])
        self.provides = provides
        self.wants = list(wants or [])
        self.resolved: dict[type, Any] = {}
        self.ctx = None
        self.log = log if log is not None else []
        self._methods = methods or {}
        self._schema = schema or {}
        self._fail_on_start = fail_on_start
        self._fail_on_stop = fail_on_stop

    async def start(self, ctx) -> None:
        if self._fail_on_start:
            raise RuntimeError(f"{self.id} refuses to start")
        self.ctx = ctx
        for api in self.wants:
            self.resolved[api] = ctx.require(api)
        self.log.append(f"start:{self.id}")

    async def stop(self) -> None:
        self.log.append(f"stop:{self.id}")
        if self._fail_on_stop:
            raise RuntimeError(f"{self.id} refuses to stop")

    def methods(self) -> dict[str, Callable[..., Any]]:
        return dict(self._methods)

    def settings_schema(self) -> dict[str, Any]:
        return dict(self._schema)


@pytest.fixture
def fake_module() -> type[FakeModule]:
    return FakeModule


@pytest.fixture
def registry_factory(tmp_path: Path) -> Callable[..., Registry]:
    """A Registry whose services all live under ``tmp_path``."""

    def build(**kwargs: Any) -> Registry:
        return Registry(
            events=EventBus(),
            storage=StorageRoot(tmp_path / "cache"),
            settings=SettingsStore(tmp_path / "config" / "settings.json"),
            methods=MethodRegistry(),
            **kwargs,
        )

    return build


@pytest.fixture
def registry(registry_factory: Callable[..., Registry]) -> Registry:
    return registry_factory()
