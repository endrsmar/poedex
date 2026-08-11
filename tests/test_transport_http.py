"""The HTTP transport.

Four things are worth testing here and they are not equally interesting:

* **Dispatch** — the boring one. A call reaches the registry, an unknown name is a
  404, a wrong argument is a 400 and not a 500.
* **The 127.0.0.1 rule** — the one that matters if it is ever wrong. This server
  reads an account's inventory, so ``assert_loopback`` refuses rather than warns,
  and the test asserts on ``0.0.0.0`` specifically because "every interface" is the
  mistake somebody actually makes.
* **The credential is unreachable.** Not by convention: by the absence of a
  registered method, plus a refusal list, and both are asserted.
* **SSE** — the event bus reaching a browser, and a slow browser not reaching the
  event bus.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from runtime.errors import PoedexError
from runtime.registry import Registry
from transports.dispatch import FORBIDDEN_METHODS, call_method, exposed_methods
from transports.http.app import _event_stream, create_app
from transports.http.server import NonLoopbackBindError, assert_loopback

pytestmark = pytest.mark.asyncio


class Refused(PoedexError):
    """Stands in for `net.RateLimited` / `poeapi.RateLimitedError`."""

    def __init__(self, retry_after: float) -> None:
        self.retry_after = retry_after
        super().__init__(f"rate limited: retry in {retry_after:.0f}s")


@pytest.fixture
def wired(registry: Registry, fake_module):
    """A registry with a couple of methods and nothing else."""

    async def echo(value: str = "hi") -> dict[str, str]:
        return {"value": value}

    async def boom() -> None:
        raise Refused(47.0)

    async def leaky() -> str:  # pragma: no cover - never reachable through a transport
        return "0123456789abcdef0123456789abcdef"

    registry.register(
        fake_module("demo", methods={"echo": echo, "boom": boom, "session_id": leaky})
    )
    return registry


@pytest.fixture
async def started(wired: Registry):
    await wired.start_all()
    yield wired
    await wired.stop_all()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:7331"
    )


# -- dispatch ------------------------------------------------------------------


async def test_a_call_reaches_the_registry(started: Registry):
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.echo", json={"value": "there"})
    assert response.status_code == 200
    assert response.json() == {"ok": True, "result": {"value": "there"}, "error": None}


async def test_an_unknown_method_is_404_and_says_which(started: Registry):
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.nope", json={})
    assert response.status_code == 404
    assert response.json()["error"]["kind"] == "UnknownMethodError"


async def test_a_wrong_argument_is_the_callers_mistake_not_a_crash(started: Registry):
    """400, not 500. A frontend passing the wrong keyword has made an error the
    backend can describe; returning 500 makes it look like the backend broke."""
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.echo", json={"nonsense": 1})
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "TypeError"


async def test_a_non_object_body_is_refused(started: Registry):
    """Keyword arguments only: a positional call over a transport breaks the day
    somebody reorders a Python signature."""
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.echo", json=["there"])
    assert response.status_code == 400
    assert response.json()["error"]["kind"] == "BadRequest"


async def test_an_empty_body_means_no_arguments(started: Registry):
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.echo", content=b"")
    assert response.json()["result"] == {"value": "hi"}


async def test_a_rate_limit_refusal_carries_the_number_to_count_down_against(
    started: Registry,
):
    """429 plus `Retry-After` plus the same figure in the body.

    The UI's `restricted` state runs a live countdown and disables the control; it
    can only do that against a number, and inventing one on the client is how a
    surface starts asking again before the limiter is ready.
    """
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.boom", json={})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "48"
    assert response.json()["error"]["retry_after"] == 47.0


async def test_meta_lists_modules_their_state_and_their_reason(started: Registry):
    async with client_for(create_app(started)) as http:
        response = await http.get("/api/meta")
    body = response.json()
    assert response.status_code == 200
    assert {module["id"] for module in body["modules"]} == {"demo"}
    assert "demo.echo" in body["methods"]
    assert body["modules"][0]["state"] == "started"


async def test_meta_is_also_reachable_as_a_method(started: Registry):
    """A transport is `call` plus events; a shell that needed a second verb for the
    handshake would be a shell the Decky transport has to grow a special case for."""
    outcome = await call_method(started, "_server.meta")
    assert outcome.ok
    assert outcome.result["profile"] == "full"


# -- the credential --------------------------------------------------------------


async def test_a_method_named_session_id_is_refused_even_when_registered(
    started: Registry,
):
    """The real `credentials` module never registers a session accessor. This test
    registers one anyway, because the property being protected is *"a transport does
    not serve a credential"*, not *"nobody has written one yet"*."""
    assert "demo.session_id" in started.methods.names()
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/credentials.session_id", json={})
    assert response.status_code == 403
    assert response.json()["error"]["kind"] == "ForbiddenMethodError"


async def test_the_real_credentials_module_exposes_no_way_to_read_the_value():
    from modules.credentials.backend.module import CredentialsModule

    names = set(CredentialsModule().methods())
    assert "session_id" in dir(CredentialsModule)  # the accessor exists in Python...
    assert names == {  # ...not here
        "status",
        "set",
        "clear",
        "mark_ok",
        "mark_rejected",
        # Phase 7's LAN pairing (SPEC §4.1). Three methods and deliberately not a
        # fourth: the credential arrives over the pairing socket from the *other*
        # machine, so none of these carries a value in and none can read one back.
        "pair_start",
        "pair_status",
        "pair_cancel",
    }


async def test_forbidden_names_never_appear_in_the_exposed_list(started: Registry):
    exposed = set(exposed_methods(started))
    assert exposed.isdisjoint(FORBIDDEN_METHODS)


# -- the local-only guard --------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
async def test_loopback_hosts_are_accepted(host: str):
    assert assert_loopback(host) == host


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.20", "example.com", ""])
async def test_everything_else_is_refused_rather_than_warned_about(host: str):
    with pytest.raises(NonLoopbackBindError):
        assert_loopback(host)


async def test_a_rebound_host_header_is_refused(started: Registry):
    """DNS rebinding: the packet arrives on 127.0.0.1 carrying someone else's name.
    A real local client never does this."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(started)),
        base_url="http://evil.test",
    ) as http:
        response = await http.post("/api/call/demo.echo", json={})
    assert response.status_code == 403
    assert response.json()["error"]["kind"] == "ForbiddenOrigin"


async def test_a_cross_origin_page_is_refused(started: Registry):
    async with client_for(create_app(started)) as http:
        response = await http.post(
            "/api/call/demo.echo", json={}, headers={"origin": "https://example.com"}
        )
    assert response.status_code == 403


async def test_a_loopback_origin_is_allowed(started: Registry):
    async with client_for(create_app(started)) as http:
        response = await http.post(
            "/api/call/demo.echo", json={}, headers={"origin": "http://localhost:5173"}
        )
    assert response.status_code == 200


async def test_no_cors_headers_are_ever_sent(started: Registry):
    """Their absence is what stops a cross-origin page reading the response."""
    async with client_for(create_app(started)) as http:
        response = await http.post("/api/call/demo.echo", json={})
    assert not any(name.startswith("access-control-") for name in response.headers)


# -- static --------------------------------------------------------------------


async def test_without_a_build_the_root_says_so(started: Registry, tmp_path: Path):
    async with client_for(create_app(started, static_dir=tmp_path / "absent")) as http:
        response = await http.get("/")
    assert response.status_code == 503
    assert "pnpm build" in response.json()["error"]["message"]


async def test_the_spa_index_is_served_for_a_client_side_route(
    started: Registry, tmp_path: Path
):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html><title>PoEDex</title>")
    async with client_for(create_app(started, static_dir=dist)) as http:
        root = await http.get("/")
        deep = await http.get("/appraisal/bag")
    assert root.status_code == deep.status_code == 200
    assert "PoEDex" in deep.text


async def test_the_static_route_does_not_shadow_the_api(started: Registry, tmp_path: Path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<!doctype html>")
    async with client_for(create_app(started, static_dir=dist)) as http:
        response = await http.get("/api/unknown")
    assert response.status_code == 404
    assert "doctype" not in response.text.lower()


# -- SSE -----------------------------------------------------------------------


class _StubRequest:
    """Just enough `Request` for the stream: it is asked whether the client left."""

    def __init__(self, disconnect_after: int = 99) -> None:
        self._asked = 0
        self._limit = disconnect_after

    async def is_disconnected(self) -> bool:
        self._asked += 1
        return self._asked > self._limit


async def test_an_event_reaches_the_stream(started: Registry):
    stream = _event_stream(started, _StubRequest(), heartbeat=5.0)
    assert await anext(stream) == b": connected\n\n"

    async def emit() -> None:
        await asyncio.sleep(0)
        await started.events.emit("sync_complete", {"rows": 23}, source="poeapi")

    task = asyncio.create_task(emit())
    chunk = await asyncio.wait_for(anext(stream), timeout=2.0)
    await task
    await stream.aclose()

    head, body, _blank = chunk.decode().split("\n", 2)
    assert head == "event: sync_complete"
    assert json.loads(body.removeprefix("data: ")) == {
        "topic": "sync_complete",
        "payload": {"rows": 23},
        "source": "poeapi",
        "at": pytest.approx(json.loads(body.removeprefix("data: "))["at"]),
    }


async def test_the_stream_heartbeats_so_an_idle_connection_is_not_reaped(
    started: Registry,
):
    stream = _event_stream(started, _StubRequest(), heartbeat=0.01)
    await anext(stream)
    assert await asyncio.wait_for(anext(stream), timeout=2.0) == b": ping\n\n"
    await stream.aclose()


async def test_a_departed_client_unsubscribes_itself(started: Registry):
    """A stream that kept its subscription after the browser closed would grow one
    dead handler per reload, and the bus awaits every handler."""
    before = started.events.subscriber_count("anything")
    stream = _event_stream(started, _StubRequest(disconnect_after=1), heartbeat=0.01)
    assert await anext(stream) == b": connected\n\n"
    assert await asyncio.wait_for(anext(stream), timeout=2.0) == b": ping\n\n"
    # The next pass asks again, is told the browser has gone, and returns.
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(stream), timeout=2.0)
    assert started.events.subscriber_count("anything") == before


async def test_a_slow_client_is_dropped_rather_than_slowing_the_bus(
    started: Registry, monkeypatch: pytest.MonkeyPatch
):
    """The bus `await`s its handlers. A browser that stopped reading must not be
    able to hold up the module that emitted the event."""
    monkeypatch.setattr("transports.http.app.EVENT_QUEUE_SIZE", 2)
    stream = _event_stream(started, _StubRequest(), heartbeat=5.0)
    await anext(stream)
    for index in range(10):
        await asyncio.wait_for(
            started.events.emit("noise", {"n": index}), timeout=1.0
        )  # never blocks
    await stream.aclose()


async def test_the_events_endpoint_declares_itself_as_a_stream(started: Registry):
    """Called through the route rather than through a client.

    An SSE endpoint never completes, and driving one through an in-process ASGI
    client means asserting on a response that is by construction still open. The
    stream's *content* is tested above against the generator directly; what is left
    to check here is that the route hands back a stream with the headers a browser
    needs — no buffering, no caching — and that is a property of the response object.
    """
    app = create_app(started, heartbeat=0.01)
    route = next(
        candidate for candidate in app.routes if getattr(candidate, "path", None) == "/api/events"
    )
    response = await route.endpoint(_StubRequest(disconnect_after=0))
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
