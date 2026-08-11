"""The Decky transport, on a machine with no Steam Deck.

`transports/decky/backend.py` never imports `decky` — `emit` is injected — which is
what makes all of this runnable here. What it cannot cover is the loader itself:
whether `_main()` really is scheduled unawaited, whether `decky.emit` reaches the
panel, whether `_unload` finishes before the SIGKILL. Those are
`docs/deck-checklist.md` items 1, 4 and 10.

The properties asserted here are the ones that would be expensive to discover on
hardware:

* the `Plugin` class **delegates to the existing method registry** and does not
  reimplement dispatch, including the parts that refuse;
* `_unload` completes promptly even when a module hangs;
* events reach `decky.emit` and the last one per topic is replayable;
* a resume is noticed from a clock jump rather than from a sleep that returned late.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar

import pytest

from runtime.context import ModuleContext
from runtime.registry import Registry
from transports.decky.backend import (
    SHUTDOWN_BUDGET_SECONDS,
    SUSPEND_JUMP_SECONDS,
    DeckyBackend,
)
from transports.dispatch import FORBIDDEN_METHODS


class DemoModule:
    """A module with one method that works, one that raises, and one that waits."""

    id = "demo"
    name = "Demo"
    kind = "feature"
    requires: ClassVar[list[str]] = []
    provides = None

    def __init__(self, *, hang: bool = False) -> None:
        self.hang = hang
        self.ctx: ModuleContext | None = None
        self.stopped = False

    async def start(self, ctx: ModuleContext) -> None:
        self.ctx = ctx

    async def stop(self) -> None:
        if self.hang:
            await asyncio.sleep(30)
        self.stopped = True

    def methods(self) -> dict[str, Any]:
        return {"echo": self.echo, "boom": self.boom}

    def settings_schema(self) -> dict[str, Any]:
        return {}

    async def echo(self, value: str = "hi") -> dict[str, str]:
        return {"value": value}

    async def boom(self) -> None:
        raise RuntimeError("the limiter refused this")

    async def announce(self) -> None:
        assert self.ctx is not None
        await self.ctx.events.emit("demo.happened", {"n": 1}, source=self.id)


class Emitter:
    """Stands in for `decky.emit`."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def __call__(self, channel: str, message: Any) -> None:
        self.sent.append((channel, message))

    def topics(self) -> list[str]:
        return [message["topic"] for _, message in self.sent]


async def build(*, hang: bool = False) -> tuple[DeckyBackend, Emitter, DemoModule]:
    module = DemoModule(hang=hang)
    registry = Registry()
    registry.register(module)
    emitter = Emitter()
    return DeckyBackend(emit=emitter, registry=registry), emitter, module


# -- dispatch is the shared one -------------------------------------------------


async def test_a_call_goes_through_the_method_registry():
    backend, _emitter, _module = await build()
    await backend.start()
    try:
        reply = await backend.call("demo.echo", {"value": "bag"})
        assert reply["ok"] is True
        assert reply["result"] == {"value": "bag"}
    finally:
        await backend.shutdown()


async def test_a_failure_arrives_structured_rather_than_as_a_string():
    """`kind` and `retry_after` are what the panel acts on.

    An exception crossing Decky's RPC becomes a string in the CEF console, which is
    why the envelope exists at all.
    """
    backend, _emitter, _module = await build()
    await backend.start()
    try:
        reply = await backend.call("demo.boom")
        assert reply["ok"] is False
        assert reply["error"]["kind"] == "RuntimeError"
        assert reply["status"] == 500
    finally:
        await backend.shutdown()


async def test_an_unknown_method_is_a_404_and_not_an_exception():
    backend, _emitter, _module = await build()
    await backend.start()
    try:
        reply = await backend.call("demo.nope")
        assert reply["ok"] is False
        assert reply["status"] == 404
    finally:
        await backend.shutdown()


@pytest.mark.parametrize("method", sorted(FORBIDDEN_METHODS))
async def test_the_forbidden_list_is_the_same_list_the_http_transport_uses(method: str):
    """Dispatch is not duplicated, so this is not a second policy to keep in sync.

    If it ever were duplicated, this is the test that would keep passing while the
    other door opened — which is exactly why the assertion is that the *shared*
    function refuses, and why `transports/decky` imports it rather than copying it.
    """
    backend, _emitter, _module = await build()
    await backend.start()
    try:
        reply = await backend.call(method)
        assert reply["ok"] is False
        assert reply["status"] == 403
        assert method not in backend.meta()["methods"]
    finally:
        await backend.shutdown()


async def test_a_call_before_start_answers_instead_of_raising():
    """The panel can open before `_main()` has finished the poe.ninja prefetch."""
    backend, _emitter, _module = await build()
    reply = await backend.call("demo.echo")
    assert reply["ok"] is False
    assert reply["error"]["kind"] == "NotStartedError"
    # The same five keys as every other reply, so a frontend never has to branch on
    # which shape it got.
    assert set(reply) == {"ok", "result", "error", "status", "retry_after"}
    assert reply["retry_after"] == 1.0


async def test_meta_reports_the_compact_profile_and_every_module():
    backend, _emitter, _module = await build()
    await backend.start()
    try:
        meta = backend.meta()
        assert meta["profile"] == "compact"
        assert [module["id"] for module in meta["modules"]] == ["demo"]
        assert "demo.echo" in meta["methods"]
    finally:
        await backend.shutdown()


# -- pushing --------------------------------------------------------------------


async def test_backend_ready_is_pushed_so_the_panel_opens_populated():
    backend, emitter, _module = await build()
    await backend.start()
    try:
        assert "backend.ready" in emitter.topics()
        assert emitter.sent[0][0] == "poedex"
    finally:
        await backend.shutdown()


async def test_a_runtime_event_reaches_decky_emit():
    backend, emitter, module = await build()
    await backend.start()
    try:
        await module.announce()
        await asyncio.sleep(0)
        assert "demo.happened" in emitter.topics()
    finally:
        await backend.shutdown()


async def test_the_last_payload_per_topic_is_replayable():
    """Panel content is unmounted whenever the QAM closes (SPEC §6.2)."""
    backend, _emitter, module = await build()
    await backend.start()
    try:
        await module.announce()
        await asyncio.sleep(0)
        assert backend.latest("demo.happened")["payload"] == {"n": 1}
        assert "backend.ready" in backend.latest()
        assert backend.latest("nothing.here") == {}
    finally:
        await backend.shutdown()


async def test_a_failing_emit_does_not_stop_the_emitter():
    """An event is a notification, not a call — the rule the bus itself follows."""

    async def broken(_channel: str, _message: Any) -> None:
        raise RuntimeError("the CEF side went away")

    module = DemoModule()
    registry = Registry()
    registry.register(module)
    backend = DeckyBackend(emit=broken, registry=registry)
    await backend.start()
    try:
        await module.announce()
        await asyncio.sleep(0)
        assert backend.latest("demo.happened")["payload"] == {"n": 1}
    finally:
        await backend.shutdown()


# -- shutting down on time ------------------------------------------------------


async def test_unload_completes_promptly():
    backend, _emitter, module = await build()
    await backend.start()
    started = time.monotonic()
    await backend.shutdown()
    assert time.monotonic() - started < 1.0
    assert module.stopped is True
    assert backend.started is False


async def test_a_module_that_hangs_does_not_take_the_shutdown_with_it():
    """Decky SIGKILLs five seconds after SIGTERM.

    Every write this project makes is atomic (`runtime/storage.py`), which is what
    makes giving up survivable rather than merely faster.
    """
    backend, _emitter, module = await build(hang=True)
    await backend.start()
    started = time.monotonic()
    await backend.shutdown()
    elapsed = time.monotonic() - started
    assert elapsed < SHUTDOWN_BUDGET_SECONDS + 1.0
    assert module.stopped is False
    assert backend.started is False


async def test_shutdown_is_idempotent():
    backend, _emitter, _module = await build()
    await backend.start()
    await backend.shutdown()
    await backend.shutdown()


async def test_start_is_idempotent():
    backend, emitter, _module = await build()
    await backend.start()
    await backend.start()
    try:
        assert emitter.topics().count("backend.ready") == 1
    finally:
        await backend.shutdown()


# -- suspend/resume -------------------------------------------------------------


async def test_a_wall_clock_jump_is_reported_as_a_resume(monkeypatch):
    """CLOCK_MONOTONIC stops during suspend and the wall clock does not.

    The two drifting apart is the signal — a stronger one than "that sleep took too
    long", because a stalled event loop moves neither. **Nothing here proves the Deck
    behaves this way**; nothing in the loader handles suspend at all, which is
    precisely why this exists and why `docs/deck-checklist.md` item 9 is the check.
    """
    import transports.decky.backend as module_under_test

    backend, emitter, _module = await build()
    monkeypatch.setattr(module_under_test, "HEARTBEAT_SECONDS", 0.01)

    wall = [1_000_000.0]
    monkeypatch.setattr(module_under_test.time, "time", lambda: wall[0])

    await backend.start()
    try:
        # One quiet tick: no jump, no event.
        await asyncio.sleep(0.05)
        assert "system.resumed" not in emitter.topics()

        # The lid was closed for two hours.
        wall[0] += SUSPEND_JUMP_SECONDS + 7200
        for _ in range(200):
            if "system.resumed" in emitter.topics():
                break
            await asyncio.sleep(0.01)
        assert "system.resumed" in emitter.topics()
        assert backend.resumes == 1
        payload = backend.latest("system.resumed")["payload"]
        assert payload["away_seconds"] > SUSPEND_JUMP_SECONDS
    finally:
        await backend.shutdown()
