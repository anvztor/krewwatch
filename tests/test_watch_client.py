"""Unit tests for WatchClient."""

from __future__ import annotations

import asyncio

import pytest

from krewwatch import WatchClient, WatchEvent


class TestWatchEvent:
    """Tests for WatchEvent dataclass."""

    def test_watch_event_fields(self):
        event = WatchEvent(
            event_type="ADDED",
            resource_type="task",
            resource_id="task-001",
            resource_version=1,
            object={"name": "test-task"},
            seq=42,
        )
        assert event.event_type == "ADDED"
        assert event.resource_type == "task"
        assert event.resource_id == "task-001"
        assert event.resource_version == 1
        assert event.object == {"name": "test-task"}
        assert event.seq == 42

    def test_watch_event_frozen(self):
        event = WatchEvent(
            event_type="ADDED",
            resource_type="task",
            resource_id="task-001",
            resource_version=1,
            object={},
        )
        with pytest.raises(AttributeError):
            event.event_type = "MODIFIED"  # type: ignore[misc]

    def test_watch_event_default_seq(self):
        event = WatchEvent(
            event_type="ADDED",
            resource_type="task",
            resource_id="task-001",
            resource_version=1,
            object={},
        )
        assert event.seq == 0


class TestWatchClientConnect:
    """Tests for WatchClient connection and event delivery."""

    async def test_receives_events_via_callback(self, fake_hub):
        hub, base_url = fake_hub
        received: list[WatchEvent] = []

        async def on_event(event: WatchEvent) -> None:
            received.append(event)

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.on_event(on_event)
        client.start()

        await asyncio.sleep(0.5)

        hub.push_event({
            "type": "ADDED",
            "resource_type": "task",
            "resource_id": "task-001",
            "resource_version": 1,
            "object": {"name": "test-task"},
        })

        for _ in range(30):
            if received:
                break
            await asyncio.sleep(0.1)

        await client.stop()

        assert len(received) == 1
        assert received[0].resource_type == "task"
        assert received[0].resource_id == "task-001"

    async def test_receives_events_via_queue(self, fake_hub):
        hub, base_url = fake_hub

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.start()

        await asyncio.sleep(0.5)

        hub.push_event({
            "type": "ADDED",
            "resource_type": "task",
            "resource_id": "task-002",
            "resource_version": 1,
            "object": {},
        })

        event = None
        for _ in range(30):
            try:
                event = client.queue.get_nowait()
                break
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)

        await client.stop()

        assert event is not None
        assert event.resource_id == "task-002"

    async def test_seq_tracking(self, fake_hub):
        hub, base_url = fake_hub
        received: list[WatchEvent] = []

        async def on_event(event: WatchEvent) -> None:
            received.append(event)

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.on_event(on_event)
        client.start()

        await asyncio.sleep(0.5)

        hub.push_event({"type": "ADDED", "resource_type": "task", "resource_id": "t1", "resource_version": 1, "object": {}, "seq": 10})
        hub.push_event({"type": "MODIFIED", "resource_type": "task", "resource_id": "t2", "resource_version": 2, "object": {}, "seq": 20})

        for _ in range(30):
            if len(received) >= 2:
                break
            await asyncio.sleep(0.1)

        await client.stop()

        assert client.last_seq == 20

    async def test_multiple_callbacks(self, fake_hub):
        hub, base_url = fake_hub
        cb1_count = 0
        cb2_count = 0

        async def cb1(event: WatchEvent) -> None:
            nonlocal cb1_count
            cb1_count += 1

        async def cb2(event: WatchEvent) -> None:
            nonlocal cb2_count
            cb2_count += 1

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.on_event(cb1)
        client.on_event(cb2)
        client.start()

        await asyncio.sleep(0.5)

        hub.push_event({"type": "ADDED", "resource_type": "task", "resource_id": "t1", "resource_version": 1, "object": {}})

        for _ in range(30):
            if cb1_count > 0 and cb2_count > 0:
                break
            await asyncio.sleep(0.1)

        await client.stop()

        assert cb1_count == 1
        assert cb2_count == 1


class TestWatchClientLifecycle:
    """Tests for WatchClient lifecycle."""

    async def test_start_stop(self, fake_hub):
        _, base_url = fake_hub

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.start()
        assert client._running is True

        await client.stop()
        assert client._running is False
        assert client._task is None

    async def test_double_start_is_noop(self, fake_hub):
        _, base_url = fake_hub

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.start()
        task1 = client._task
        client.start()  # should be no-op
        task2 = client._task

        await client.stop()

        assert task1 is task2

    async def test_callback_error_does_not_crash(self, fake_hub):
        hub, base_url = fake_hub
        good_received: list[WatchEvent] = []

        async def bad_callback(event: WatchEvent) -> None:
            raise RuntimeError("callback error")

        async def good_callback(event: WatchEvent) -> None:
            good_received.append(event)

        client = WatchClient(base_url=base_url, api_key="test-key")
        client.on_event(bad_callback)
        client.on_event(good_callback)
        client.start()

        await asyncio.sleep(0.5)

        hub.push_event({"type": "ADDED", "resource_type": "task", "resource_id": "t1", "resource_version": 1, "object": {}})

        for _ in range(30):
            if good_received:
                break
            await asyncio.sleep(0.1)

        await client.stop()

        assert len(good_received) == 1
