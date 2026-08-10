"""Event bus."""

from __future__ import annotations

import asyncio

import pytest

from runtime.events import Event, EventBus


async def test_exact_topic_delivery():
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("zone_changed", seen.append)

    await bus.emit("zone_changed", {"zone": "Hideout"}, source="gamelog")

    assert len(seen) == 1
    assert seen[0].topic == "zone_changed"
    assert seen[0].payload == {"zone": "Hideout"}
    assert seen[0].source == "gamelog"
    assert seen[0].at > 0


async def test_other_topics_are_not_delivered():
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("sync_complete", seen.append)
    await bus.emit("zone_changed")
    assert seen == []


async def test_async_and_sync_handlers_both_run():
    bus = EventBus()
    order: list[str] = []

    async def slow(event: Event) -> None:
        await asyncio.sleep(0)
        order.append("async")

    bus.subscribe("t", slow)
    bus.subscribe("t", lambda event: order.append("sync"))

    await bus.emit("t")
    assert sorted(order) == ["async", "sync"]


async def test_wildcard_and_prefix_patterns():
    bus = EventBus()
    everything: list[str] = []
    prefixed: list[str] = []
    bus.subscribe("*", lambda e: everything.append(e.topic))
    bus.subscribe("credentials.*", lambda e: prefixed.append(e.topic))

    await bus.emit("credentials.changed")
    await bus.emit("zone_changed")

    assert everything == ["credentials.changed", "zone_changed"]
    assert prefixed == ["credentials.changed"]


async def test_unsubscribe_via_returned_handle():
    bus = EventBus()
    seen: list[Event] = []
    off = bus.subscribe("t", seen.append)
    await bus.emit("t")
    off()
    await bus.emit("t")
    assert len(seen) == 1
    assert bus.subscriber_count("t") == 0


async def test_unsubscribe_is_idempotent():
    bus = EventBus()
    handler = []
    off = bus.subscribe("t", handler.append)
    off()
    off()  # must not raise
    bus.unsubscribe("never-subscribed", handler.append)


async def test_a_failing_handler_does_not_stop_the_others():
    bus = EventBus()
    seen: list[str] = []

    def explode(event: Event) -> None:
        raise ValueError("handler is broken")

    bus.subscribe("t", explode)
    bus.subscribe("t", lambda e: seen.append("ok"))

    await bus.emit("t")  # must not raise: an event is a notification, not a call

    assert seen == ["ok"]
    assert bus.handler_errors == 1


async def test_wait_for_resolves_on_a_matching_event():
    bus = EventBus()
    waiter = asyncio.create_task(bus.wait_for("sync_complete", timeout=1))
    await asyncio.sleep(0)
    await bus.emit("sync_complete", {"items": 3})
    event = await waiter
    assert event.payload == {"items": 3}


async def test_wait_for_times_out():
    bus = EventBus()
    with pytest.raises(asyncio.TimeoutError):
        await bus.wait_for("never", timeout=0.01)


async def test_payload_is_copied_not_aliased():
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe("t", seen.append)
    payload = {"n": 1}
    await bus.emit("t", payload)
    payload["n"] = 2
    assert seen[0].payload == {"n": 1}


async def test_unsubscribe_all_by_owner():
    bus = EventBus()

    class Owner:
        def __init__(self) -> None:
            self.seen: list[Event] = []

        def handle(self, event: Event) -> None:
            self.seen.append(event)

    owner = Owner()
    other: list[Event] = []
    bus.subscribe("t", owner.handle)
    bus.subscribe("t", other.append)

    bus.unsubscribe_all(owner)
    await bus.emit("t")

    assert owner.seen == []
    assert len(other) == 1
