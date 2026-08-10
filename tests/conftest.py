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
from runtime.settings import SETTINGS_FILENAME, SettingsStore
from runtime.storage import StorageRoot

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "poeapi"
PRICE_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "prices"
APPRAISAL_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "appraisal"

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


@pytest.fixture
def league_override() -> str:
    """Which league the suite pins ``prices`` to. Override it in a test module.

    ``""`` means "no override": the league then comes from the bag, which is the
    product's real path and what ``tests/test_league.py`` exercises.
    """
    return "Standard"


@pytest.fixture(autouse=True)
def _pin_price_league(tmp_path: Path, poedex_home: Path, league_override: str) -> None:
    """Write ``prices.league`` into the settings file every registry fixture reads.

    Until the league fix, ``"Standard"`` was ``prices``' silent default and every
    test in this suite was written on top of it — against Standard fixtures, with no
    test ever naming a league. Now that a bag's own league decides and the setting is
    an override, that assumption has to be *stated* rather than inherited: this pins
    it, so those tests keep testing what they were written to test instead of
    accidentally becoming tests of the new resolution order.

    The new resolution order gets its own file, which sets ``league_override`` to
    ``""`` and takes the fixtures' Allflame character out for a walk.
    """
    path = tmp_path / "config" / SETTINGS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {"prices": {"league": league_override}} if league_override else {}
    path.write_text(json.dumps(values), "utf-8")


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
def registry_factory(tmp_path: Path, _pin_price_league: None) -> Callable[..., Registry]:
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


def price_payload(name: str) -> Any:
    """A recorded poe.ninja or trade response. See ``tests/fixtures/prices/README.md``."""
    return json.loads((PRICE_FIXTURES / name).read_text("utf-8"))


def bag_payload(name: str) -> Any:
    """A ``get-items`` body, from whichever fixture directory owns it.

    Three phases now ship a bag and each keeps it next to the tests that justify it
    — `poeapi`'s shape fixture, `prices`' pricing exerciser, `appraisal`'s loot bag.
    The scripted server should not have to know which is which.
    """
    for directory in (FIXTURES, PRICE_FIXTURES, APPRAISAL_FIXTURES):
        candidate = directory / name
        if candidate.is_file():
            return json.loads(candidate.read_text("utf-8"))
    raise FileNotFoundError(f"no bag fixture named {name!r}")


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


# Every poe.ninja table this suite can answer, by (kind, type) as they appear on the
# wire. Keyed the way the real routes are so a test can assert the URL, not a nickname.
NINJA_TABLES = {
    ("exchange", "Currency"): "exchange-currency.json",
    ("exchange", "Fragment"): "exchange-fragment.json",
    ("exchange", "Scarab"): "exchange-scarab.json",
    ("exchange", "Fossil"): "exchange-fossil.json",
    ("exchange", "Essence"): "exchange-essence.json",
    ("exchange", "Oil"): "exchange-oil.json",
    ("exchange", "DeliriumOrb"): "exchange-deliriumorb.json",
    ("exchange", "DivinationCard"): "exchange-divinationcard.json",
    ("item", "Incubator"): "item-incubator.json",
    ("item", "Map"): "item-map.json",
    ("item", "UniqueMap"): "item-uniquemap.json",
    ("item", "UniqueWeapon"): "item-uniqueweapon.json",
    ("item", "UniqueArmour"): "item-uniquearmour.json",
    ("item", "UniqueAccessory"): "item-uniqueaccessory.json",
    ("item", "UniqueFlask"): "item-uniqueflask.json",
    ("item", "UniqueJewel"): "item-uniquejewel.json",
}

NINJA_HEADERS = {
    # Verbatim from a live response, 2026-08-10. Note the absence of any
    # X-Rate-Limit-* header: that absence is what the courtesy budget exists for.
    "cache-control": "public, max-age=1800, stale-while-revalidate=300, stale-if-error=86400",
    "content-type": "application/json",
}

TRADE_HEADERS = {
    "x-rate-limit-policy": "trade-search-request-limit",
    "x-rate-limit-rules": "Ip",
    "x-rate-limit-ip": "5:10:60,15:60:300,30:300:1800,600:21600:3600",
    "x-rate-limit-ip-state": "1:10:0,1:60:0,1:300:0,1:21600:0",
}

TRADE_FETCH_HEADERS = {
    "x-rate-limit-policy": "trade-fetch-request-limit",
    "x-rate-limit-rules": "Ip",
    "x-rate-limit-ip": "12:4:10,16:12:300,50:300:300,1000:21600:1800",
    "x-rate-limit-ip-state": "1:4:0,1:12:0,1:300:0,1:21600:0",
}


class Server:
    """A scripted pathofexile.com **and** poe.ninja. Answers from fixtures.

    One handler for both hosts on purpose: the production client is also one client
    for both, and routing by hostname is exactly the behaviour under test — the
    credential must reach one host and not the other, and their rate-limit buckets
    must stay apart.

    ``status`` and ``header_file`` are the two knobs for the GGG side: a test sets
    them to make the next response a 403, a 429, or a 500. ``ninja_status`` and
    ``etag`` are the equivalents for the poe.ninja side.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.header_file = "headers-items-authenticated.json"
        self.error_body: Any = {"error": {"code": 1, "message": "Forbidden"}}
        self.ninja_status = 200
        self.etag = "W/fixture-1"
        """What poe.ninja claims its tables are at. Change it to simulate a refresh;
        leave it and a conditional request gets a 304."""

        self.bag_fixture = "get-items.json"
        self.characters: Any = payload("get-characters.json")
        """The roster, as an attribute so a test can empty it or move a character to
        another league. Which league a character is in is now load-bearing — it is
        what every price is denominated in — so it has to be a knob, not a constant."""

        self._payloads = {
            "/character-window/get-stash-items": payload("get-stash-items.json"),
        }

    # -- routing ---------------------------------------------------------------

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "poe.ninja":
            return self._ninja(request)
        if request.url.path.startswith("/api/trade/"):
            return self._trade(request)
        return self._ggg(request)

    def _ggg(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(
                self.status, json=self.error_body, headers=headers(self.header_file)
            )
        path = request.url.path
        if path == "/character-window/get-characters":
            body = self.characters
        elif path == "/character-window/get-items":
            body = bag_payload(self.bag_fixture)
        elif path == "/character-window/get-stash-items" and request.url.params.get("tabs") == "1":
            body = payload("get-stash-tabs.json")
        else:
            body = self._payloads.get(path, {})
        return httpx.Response(200, json=body, headers=headers(self.header_file))

    def _ninja(self, request: httpx.Request) -> httpx.Response:
        if self.ninja_status != 200:
            return httpx.Response(self.ninja_status, json={"error": "no"}, headers=NINJA_HEADERS)
        path = request.url.path
        if path.endswith("/economy/leagues"):
            return self._conditional(request, price_payload("leagues.json"))
        kind = "exchange" if "/exchange/" in path else "item"
        key = (kind, request.url.params.get("type", ""))
        name = NINJA_TABLES.get(key)
        if name is None:
            return httpx.Response(404, json={"error": "unknown type"}, headers=NINJA_HEADERS)
        return self._conditional(request, price_payload(name))

    def _conditional(self, request: httpx.Request, body: Any) -> httpx.Response:
        sent = request.headers.get("if-none-match")
        response_headers = {**NINJA_HEADERS, "etag": self.etag}
        if sent and sent == self.etag:
            return httpx.Response(304, headers=response_headers)
        return httpx.Response(200, json=body, headers=response_headers)

    def _trade(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/trade/data/stats":
            return httpx.Response(200, json=price_payload("trade-stats.json"))
        if path.startswith("/api/trade/search/"):
            return httpx.Response(
                200, json=price_payload("trade-search.json"), headers=TRADE_HEADERS
            )
        if path.startswith("/api/trade/fetch/"):
            return httpx.Response(
                200, json=price_payload("trade-fetch.json"), headers=TRADE_FETCH_HEADERS
            )
        return httpx.Response(404, json={"error": "unknown trade route"})

    # -- assertions ------------------------------------------------------------

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def hosts(self) -> list[str]:
        return [r.url.host for r in self.requests]

    def to_host(self, host: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == host]

    def trade_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith("/api/trade/")]


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
async def stack_factory(tmp_path: Path, registry: Registry, server: Server, clock, cache_clock):
    """Build the offline PoE stack, optionally with extra modules, and start it.

    A factory rather than a fixture so `prices` can join *before* ``start_all`` —
    the registry resolves dependencies once, and a module bolted on afterwards would
    be exercising a path the product never takes.
    """
    from modules.credentials.backend.module import CredentialsModule
    from modules.credentials.backend.store import SessionStore
    from modules.net.backend.client import NetClient, build_user_agent
    from modules.net.backend.module import NetModule
    from modules.net.backend.ratelimit import RateLimiter
    from modules.poeapi.backend.cache import ResponseCache
    from modules.poeapi.backend.module import PoeApiModule
    from runtime.storage import Storage

    built: dict[str, Any] = {}

    async def build(*extra: Any) -> Registry:
        from modules.credentials.backend.api import CredentialsApi

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
        built["client"] = client
        cache = ResponseCache(Storage(tmp_path / "cache", "poeapi"), clock=cache_clock)
        for module in (credentials, NetModule(client=client), PoeApiModule(cache=cache), *extra):
            registry.register(module)
        await registry.start_all()
        await registry.api(CredentialsApi).set(SESSION_VALUE, ACCOUNT)
        return registry

    yield build
    client = built.get("client")
    if client is not None:
        await client.aclose()


@pytest.fixture
async def stack(stack_factory, registry: Registry):
    """`credentials` + `net` + `poeapi`, started, with no socket anywhere."""
    yield await stack_factory()
    await registry.stop_all()


@pytest.fixture
def api(stack: Registry):
    from modules.poeapi.backend.api import PoeApi

    return stack.api(PoeApi)


# -- the offline pricing stack --------------------------------------------------


@pytest.fixture
async def prices_module(cache_clock: FakeClock):
    """An unstarted `prices` on the test clock, with the background prefetch off.

    Off because a test that raced its own fixture would be flaky in exactly the way
    that teaches people to add sleeps; the ``priced`` fixture awaits the refresh
    explicitly instead. ``test_prices_module`` covers the real start-time prefetch.
    """
    from modules.prices.backend.module import PricesModule

    return PricesModule(clock=cache_clock, prefetch=False)


@pytest.fixture
async def priced_stack(stack_factory, registry: Registry, server: Server, prices_module):
    """The whole stack including `prices`, with the fixture bag and tables loaded."""
    server.bag_fixture = "bag.json"
    result = await stack_factory(prices_module)
    await prices_module.refresh()
    yield result
    await registry.stop_all()


@pytest.fixture
def priced(priced_stack: Registry):
    from modules.prices.backend.api import PricesApi

    return priced_stack.api(PricesApi)


# -- the offline appraisal stack ------------------------------------------------


@pytest.fixture
def appraisal_module():
    from modules.appraisal.backend.module import AppraisalModule

    return AppraisalModule()


@pytest.fixture
async def appraised_stack(
    stack_factory, registry: Registry, server: Server, prices_module, appraisal_module
):
    """The whole stack through `appraisal`, on the Phase 4 loot bag.

    ``tests/fixtures/appraisal/loot-bag.json`` rather than `prices`' ``bag.json``:
    the pricing fixture's rares carry ``ilvl: 0`` and one mod each, so the tier-2
    gate has nothing to read and the two strictness levels cannot be shown to
    diverge on it. See that directory's README for what the loot bag is and is not.
    """
    server.bag_fixture = "loot-bag.json"
    result = await stack_factory(prices_module, appraisal_module)
    await prices_module.refresh()
    yield result
    await registry.stop_all()


@pytest.fixture
def appraiser(appraised_stack: Registry):
    from modules.appraisal.backend.api import AppraisalApi

    return appraised_stack.api(AppraisalApi)


@pytest.fixture
async def loot(appraised_stack: Registry):
    """The normalized loot bag, bag slots only — what an appraisal takes."""
    from modules.poeapi.backend.api import PoeApi, Source

    bag = await appraised_stack.api(PoeApi).get_items()
    return bag.by_source(Source.BAG)
