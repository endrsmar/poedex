"""Shared fixtures.

Every test is offline and writes only inside ``tmp_path``. Nothing here touches the
real ``~/.config/poedex``; the ``poedex_home`` fixture is autouse so a test that
forgets to isolate itself still cannot.

From Phase 2 this file also owns the **offline PoE stack**: a `credentials` + `net` +
`poeapi` registry whose only transport is an ``httpx.MockTransport`` answering from
``tests/fixtures/poeapi/``. It lives here rather than in one test module so the
several modules that need it do not have to import each other's fixtures, and so
that there is exactly one place to look for "how do the tests avoid the network".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.events import EventBus
from runtime.methods import MethodRegistry
from runtime.registry import Registry
from runtime.secrets import clear_secrets
from runtime.settings import SettingsStore
from runtime.storage import StorageRoot

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "poeapi"

SESSION_VALUE = "0123456789abcdef0123456789abcdef"
"""A syntactically valid POESESSID that has never been a real one."""

ACCOUNT = "ExampleAccount"


@pytest.fixture(autouse=True)
def poedex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every path helper at a throwaway directory."""
    monkeypatch.setenv("POEDEX_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("POEDEX_CACHE_DIR", str(tmp_path / "cache"))
    for leaked in (
        "DECKY_PLUGIN_SETTINGS_DIR",
        "DECKY_PLUGIN_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "POEDEX_GAMELOG_FROM_START",
    ):
        monkeypatch.delenv(leaked, raising=False)
    # `gamelog` starts a watcher whenever the registry starts, and left alone it
    # would probe the developer's real ~/.steam. Point it at a path that will never
    # exist: the watcher then sits in `waiting` and touches nothing outside tmp_path.
    monkeypatch.setenv("POEDEX_GAMELOG_PATH", str(tmp_path / "no-such-Client.txt"))
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


# -- the offline PoE stack ------------------------------------------------------


def payload(name: str) -> Any:
    """A recorded response body. See ``tests/fixtures/poeapi/README.md``."""
    return json.loads((FIXTURES / name).read_text("utf-8"))


def headers(name: str) -> dict[str, str]:
    """A recorded set of rate-limit response headers."""
    return json.loads((FIXTURES / name).read_text("utf-8"))


class FakeClock:
    """A clock the test drives. Nothing in the suite ever sleeps for real."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class FakeCredentials:
    """Just enough of ``CredentialsApi`` for `net`, which only needs the value."""

    def __init__(self, value: str | None = SESSION_VALUE) -> None:
        self.value = value

    async def session_id(self) -> str | None:
        return self.value


class Server:
    """A scripted pathofexile.com. Records every request; answers from fixtures.

    ``status`` and ``header_file`` are the two knobs: a test sets them to make the
    next response a 403, a 429, or a 500.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.header_file = "headers-items-authenticated.json"
        self.error_body: Any = {"error": {"code": 1, "message": "Forbidden"}}
        self._payloads = {
            "/character-window/get-characters": payload("get-characters.json"),
            "/character-window/get-items": payload("get-items.json"),
            "/character-window/get-stash-items": payload("get-stash-items.json"),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(
                self.status, json=self.error_body, headers=headers(self.header_file)
            )
        path = request.url.path
        if path == "/character-window/get-stash-items" and request.url.params.get("tabs") == "1":
            body = payload("get-stash-tabs.json")
        else:
            body = self._payloads.get(path, {})
        return httpx.Response(200, json=body, headers=headers(self.header_file))

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


@pytest.fixture
def server() -> Server:
    return Server()


@pytest.fixture
def clock() -> FakeClock:
    """Monotonic time, for the rate limiter."""
    return FakeClock()


@pytest.fixture
def cache_clock() -> FakeClock:
    """Wall-clock epoch time, for the response cache. Separate on purpose."""
    return FakeClock(start=1_760_000_000.0)


@pytest.fixture
async def stack(tmp_path: Path, registry: Registry, server: Server, clock, cache_clock):
    """`credentials` + `net` + `poeapi`, started, with no socket anywhere."""
    from modules.credentials.backend.api import CredentialsApi
    from modules.credentials.backend.module import CredentialsModule
    from modules.credentials.backend.store import SessionStore
    from modules.net.backend.client import NetClient, build_user_agent
    from modules.net.backend.module import NetModule
    from modules.net.backend.ratelimit import RateLimiter
    from modules.poeapi.backend.cache import ResponseCache
    from modules.poeapi.backend.module import PoeApiModule
    from runtime.storage import Storage

    # The real `credentials` module, not a stand-in: the client has to read the
    # credential through the same object the rest of the stack mutates, or a test
    # that clears the session would still see requests go out with a cookie.
    credentials = CredentialsModule(store=SessionStore(tmp_path / "config" / "session.json"))
    client = NetClient(
        limiter=RateLimiter(clock=clock),
        user_agent=build_user_agent("0.1.0", "test@example.com"),
        credentials=credentials,
        transport=httpx.MockTransport(server),
    )
    modules = (
        credentials,
        NetModule(client=client),
        PoeApiModule(cache=ResponseCache(Storage(tmp_path / "cache", "poeapi"), clock=cache_clock)),
    )
    for module in modules:
        registry.register(module)
    await registry.start_all()
    await registry.api(CredentialsApi).set(SESSION_VALUE, ACCOUNT)
    yield registry
    await registry.stop_all()
    await client.aclose()


@pytest.fixture
def api(stack: Registry):
    from modules.poeapi.backend.api import PoeApi

    return stack.api(PoeApi)
