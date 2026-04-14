"""Unit tests for SSEWatcher."""

from __future__ import annotations

import asyncio

import pytest

from krewwatch import SSEWatcher


class TestSSEWatcherPoll:
    """Tests for the polling path."""

    async def test_poll_pending_calls_callback(self, fake_hub):
        hub, base_url = fake_hub
        received = []

        async def on_invocation(payload: dict) -> dict | None:
            received.append(payload)
            return {"text": "ok"}

        hub.add_pending("alice", "coder", {
            "invocation_id": "inv-001",
            "method": "tasks/send",
            "params": {"message": {"parts": [{"text": "hello"}]}},
        })

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        await watcher.poll_pending()

        assert len(received) == 1
        assert received[0]["id"] == "inv-001"
        assert received[0]["agent_name"] == "coder"

    async def test_poll_posts_response(self, fake_hub):
        hub, base_url = fake_hub

        async def on_invocation(payload: dict) -> dict | None:
            return {"text": "done"}

        hub.add_pending("alice", "coder", {
            "invocation_id": "inv-002",
            "method": "tasks/send",
            "params": {},
        })

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        await watcher.poll_pending()

        assert len(hub.responses) == 1
        assert hub.responses[0]["invocation_id"] == "inv-002"
        assert hub.responses[0]["result"] == {"text": "done"}

    async def test_poll_posts_error_on_callback_failure(self, fake_hub):
        hub, base_url = fake_hub

        async def on_invocation(payload: dict) -> dict | None:
            raise RuntimeError("agent crashed")

        hub.add_pending("alice", "coder", {
            "invocation_id": "inv-003",
            "method": "tasks/send",
            "params": {},
        })

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        await watcher.poll_pending()

        assert len(hub.responses) == 1
        assert "error" in hub.responses[0]
        assert "agent crashed" in hub.responses[0]["error"]


class TestSSEWatcherDedup:
    """Tests for deduplication logic."""

    async def test_same_invocation_processed_once(self, fake_hub):
        hub, base_url = fake_hub
        call_count = 0

        async def on_invocation(payload: dict) -> dict | None:
            nonlocal call_count
            call_count += 1
            return {"text": "ok"}

        # Add same invocation twice
        for _ in range(2):
            hub.add_pending("alice", "coder", {
                "invocation_id": "inv-dup",
                "method": "tasks/send",
                "params": {},
            })

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        await watcher.poll_pending()

        assert call_count == 1

    async def test_dedup_set_caps_at_1000(self, fake_hub):
        _, base_url = fake_hub
        received = []

        async def on_invocation(payload: dict) -> dict | None:
            received.append(payload["id"])
            return None

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        # Simulate 1001 unique invocations via _handle_event directly
        for i in range(1001):
            await watcher._handle_event({
                "id": f"inv-{i:04d}",
                "owner": "alice",
                "agent_name": "coder",
            })

        # After exceeding 1000, the set should have been trimmed to ~500
        assert len(watcher._processed) <= 600

    async def test_ignores_wrong_owner(self, fake_hub):
        _, base_url = fake_hub
        received = []

        async def on_invocation(payload: dict) -> dict | None:
            received.append(payload)
            return None

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        await watcher._handle_event({
            "id": "inv-wrong",
            "owner": "bob",
            "agent_name": "coder",
        })

        assert len(received) == 0

    async def test_ignores_wrong_agent(self, fake_hub):
        _, base_url = fake_hub
        received = []

        async def on_invocation(payload: dict) -> dict | None:
            received.append(payload)
            return None

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        await watcher._handle_event({
            "id": "inv-wrong",
            "owner": "alice",
            "agent_name": "designer",
        })

        assert len(received) == 0


class TestSSEWatcherSSEStream:
    """Tests for the SSE streaming path."""

    async def test_sse_stream_delivers_events(self, fake_hub):
        hub, base_url = fake_hub
        received = []

        async def on_invocation(payload: dict) -> dict | None:
            received.append(payload)
            return {"text": "ok"}

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        watcher.start()

        await asyncio.sleep(0.5)

        hub.push_event({
            "resource_type": "a2a_invocation",
            "object": {
                "id": "inv-sse-001",
                "owner": "alice",
                "agent_name": "coder",
                "method": "tasks/send",
                "params": {},
            },
        })

        # Wait for delivery
        for _ in range(30):
            if received:
                break
            await asyncio.sleep(0.1)

        await watcher.stop()

        assert len(received) == 1
        assert received[0]["id"] == "inv-sse-001"


class TestSSEWatcherLifecycle:
    """Tests for start/stop lifecycle."""

    async def test_start_stop(self, fake_hub):
        _, base_url = fake_hub

        async def on_invocation(payload: dict) -> dict | None:
            return None

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token="test-jwt",
            owner="alice",
            agent_names=["coder"],
            on_invocation=on_invocation,
        )

        watcher.start()
        assert watcher._running is True
        assert watcher._poll_task is not None
        assert watcher._sse_task is not None

        await watcher.stop()
        assert watcher._running is False
