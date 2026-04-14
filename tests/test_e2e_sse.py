"""End-to-end test for krewwatch against a real krewhub subprocess.

Exercises:
1. Starts krewhub subprocess on a random port
2. Tests WatchClient SSE connection lifecycle
3. Tests SSEWatcher polling + SSE connection lifecycle

Requires krewhub to be available at ../../krewhub relative to this file.
"""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path
from time import monotonic

import httpx
import pytest

from krewwatch import SSEWatcher, WatchClient, WatchEvent

KREWHUB_PROJECT_PATH = Path(__file__).resolve().parents[2] / "krewhub"
KREWHUB_BIN_PATH = KREWHUB_PROJECT_PATH / ".venv" / "bin" / "krewhub"


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"local socket bind not permitted: {exc}")
        sock.listen(1)
        return int(sock.getsockname()[1])


def _krewhub_command() -> list[str]:
    if KREWHUB_BIN_PATH.exists():
        return [str(KREWHUB_BIN_PATH)]
    return ["uv", "run", "--project", str(KREWHUB_PROJECT_PATH), "krewhub"]


async def _wait_for_server(base_url: str, timeout_seconds: float = 30.0) -> None:
    deadline = monotonic() + timeout_seconds
    async with httpx.AsyncClient(base_url=base_url, timeout=1.0) as client:
        while monotonic() < deadline:
            try:
                resp = await client.get("/openapi.json")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)
    raise AssertionError(f"server at {base_url} did not become ready")


@pytest.fixture
async def krewhub_server(tmp_path):
    """Start a real krewhub subprocess on a random port."""
    if not KREWHUB_PROJECT_PATH.exists():
        pytest.skip("krewhub project not found at ../../krewhub")

    port = _get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_key = "e2e-krewwatch-key"
    db_path = tmp_path / "krewwatch-e2e.sqlite3"

    proc = await asyncio.create_subprocess_exec(
        *_krewhub_command(),
        cwd=str(KREWHUB_PROJECT_PATH),
        env={
            **os.environ,
            "KREWHUB_HOST": "127.0.0.1",
            "KREWHUB_PORT": str(port),
            "KREWHUB_DATABASE_PATH": str(db_path),
            "KREWHUB_API_KEY": api_key,
        },
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        await _wait_for_server(base_url)
        yield base_url, api_key
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@pytest.mark.skipif(
    not KREWHUB_PROJECT_PATH.exists(),
    reason="krewhub project not available",
)
class TestE2EWatchClient:
    """E2E: WatchClient against real krewhub."""

    async def test_watch_client_connects_and_stops(self, krewhub_server):
        base_url, api_key = krewhub_server
        received: list[WatchEvent] = []

        async def on_event(event: WatchEvent) -> None:
            received.append(event)

        client = WatchClient(
            base_url=base_url,
            api_key=api_key,
        )
        client.on_event(on_event)
        client.start()

        # Give it time to connect and stream
        await asyncio.sleep(2)
        await client.stop()

        # Verifies SSE connection lifecycle works against real krewhub
        assert client._running is False
        assert client.last_seq >= 0


@pytest.mark.skipif(
    not KREWHUB_PROJECT_PATH.exists(),
    reason="krewhub project not available",
)
class TestE2ESSEWatcher:
    """E2E: SSEWatcher against real krewhub."""

    async def test_sse_watcher_poll_no_crash(self, krewhub_server):
        base_url, api_key = krewhub_server
        received: list[dict] = []

        async def on_invocation(payload: dict) -> dict | None:
            received.append(payload)
            return {"text": "e2e-ok"}

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token=api_key,
            owner="e2e-owner",
            agent_names=["e2e-agent"],
            on_invocation=on_invocation,
            poll_interval=1.0,
        )

        # Poll once — no pending invocations expected, should not crash
        await watcher.poll_pending()
        assert len(received) == 0

    async def test_sse_watcher_full_lifecycle(self, krewhub_server):
        base_url, api_key = krewhub_server

        async def on_invocation(payload: dict) -> dict | None:
            return None

        watcher = SSEWatcher(
            krewhub_url=base_url,
            jwt_token=api_key,
            owner="e2e-owner",
            agent_names=["e2e-agent"],
            on_invocation=on_invocation,
        )

        watcher.start()
        await asyncio.sleep(2)
        await watcher.stop()

        # Verifies start/poll/SSE/stop lifecycle works against real krewhub
        assert watcher._running is False
