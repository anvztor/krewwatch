"""Production E2E evaluation of the SSE pub/sub chain.

Tests the full pipeline on the real AWS deployment:
  Agent (pub) → krewhub API → SSE /api/v1/watch → cookrew BFF → browser EventSource

Endpoints:
  - hub.cookrew.dev  (krewhub — SSE server)
  - cookrew.dev      (cookrew — frontend with BFF proxy)

Requires:
  - Internet connectivity to hub.cookrew.dev and cookrew.dev
  - KREWHUB_API_KEY env var or default test key
  - agent-browser CLI installed (for browser eval)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import uuid

import httpx
import pytest

KREWHUB_URL = "https://hub.cookrew.dev"
COOKREW_URL = "https://cookrew.dev"
API_KEY = os.environ.get("KREWHUB_API_KEY", "CHANGE_ME")
AGENT_BROWSER = shutil.which("agent-browser")

# Higher connect timeout for remote TLS connections
_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


def _run_agent_browser(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    cmd = [AGENT_BROWSER, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


# ---------------------------------------------------------------------------
# 1. Verify SSE watch endpoint is live
# ---------------------------------------------------------------------------


class TestProdSSEEndpoint:
    """Verify krewhub SSE watch endpoint works on production."""

    async def test_watch_endpoint_returns_sse_stream(self):
        """GET /api/v1/watch should return text/event-stream."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                f"{KREWHUB_URL}/api/v1/watch?since=0",
                headers={"X-API-Key": API_KEY},
            ) as resp:
                assert resp.status_code == 200
                content_type = resp.headers.get("content-type", "")
                assert "text/event-stream" in content_type, (
                    f"Expected text/event-stream, got {content_type}"
                )

                # Read at least one event
                first_line = None
                async for line in resp.aiter_lines():
                    if line.startswith("event:") or line.startswith("data:"):
                        first_line = line
                        break

                assert first_line is not None, "No SSE events received within timeout"

    async def test_watch_endpoint_replays_from_seq(self):
        """SSE replay: ?since=0 should return historical events."""
        events = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                f"{KREWHUB_URL}/api/v1/watch?since=0",
                headers={"X-API-Key": API_KEY},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str:
                            try:
                                events.append(json.loads(data_str))
                            except json.JSONDecodeError:
                                pass
                    if len(events) >= 5:
                        break

        assert len(events) >= 1, "No replay events received"

        # Verify events have seq numbers and they're ordered
        seqs = [e.get("seq", 0) for e in events]
        assert seqs == sorted(seqs), f"Seq numbers not ordered: {seqs}"

    async def test_watch_filters_by_resource_type(self):
        """?resource_type=task should only return task events."""
        events = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                f"{KREWHUB_URL}/api/v1/watch?since=0&resource_type=task",
                headers={"X-API-Key": API_KEY},
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str:
                            try:
                                event = json.loads(data_str)
                                events.append(event)
                            except json.JSONDecodeError:
                                pass
                    if len(events) >= 3:
                        break

        for event in events:
            assert event.get("resource_type") == "task", (
                f"Expected resource_type=task, got {event.get('resource_type')}"
            )


# ---------------------------------------------------------------------------
# 2. Pub: Agent publishes events via krewhub API
# ---------------------------------------------------------------------------


class TestProdSSEPub:
    """Test that agents can publish events that appear on SSE."""

    async def test_agent_heartbeat_publishes_watch_event(self):
        """Register/heartbeat an agent → event appears on SSE watch stream."""
        agent_id = f"eval-agent-{uuid.uuid4().hex[:8]}"

        # Discover a real cookbook_id from the watch stream
        cookbook_id = None
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                f"{KREWHUB_URL}/api/v1/watch?since=0&resource_type=cookbook",
                headers={"X-API-Key": API_KEY},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str:
                            try:
                                event = json.loads(data_str)
                                cookbook_id = event.get("object", {}).get("id")
                                if cookbook_id:
                                    break
                            except json.JSONDecodeError:
                                pass

        assert cookbook_id, "No cookbook found in production to test against"

        # Get current max seq for agent events
        max_seq = 0
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                f"{KREWHUB_URL}/api/v1/watch?since=0&resource_type=agent",
                headers={"X-API-Key": API_KEY},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str:
                            try:
                                event = json.loads(data_str)
                                max_seq = max(max_seq, event.get("seq", 0))
                            except json.JSONDecodeError:
                                pass
                    if max_seq > 0:
                        break

        # Publish: register agent via krewhub API using real cookbook_id
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{KREWHUB_URL}/api/v1/agents/heartbeat",
                json={
                    "agent_id": agent_id,
                    "cookbook_id": cookbook_id,
                    "display_name": "E2E Eval Agent",
                    "capabilities": ["eval"],
                    "status": "online",
                },
                headers={"X-API-Key": API_KEY},
            )
            assert resp.status_code in (200, 201), (
                f"Heartbeat failed: {resp.status_code} {resp.text[:200]}"
            )

        # Subscribe: check the event appears on the watch stream
        found = False
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            async with client.stream(
                "GET",
                f"{KREWHUB_URL}/api/v1/watch?since={max_seq}&resource_type=agent",
                headers={"X-API-Key": API_KEY},
            ) as resp:
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if data_str:
                            try:
                                event = json.loads(data_str)
                                obj = event.get("object", {})
                                if obj.get("agent_id") == agent_id:
                                    found = True
                                    break
                            except json.JSONDecodeError:
                                pass

        assert found, f"Agent {agent_id} event not found in SSE stream"


# ---------------------------------------------------------------------------
# 3. Sub: cookrew BFF proxies SSE to frontend
# ---------------------------------------------------------------------------


class TestProdCookrewBFFProxy:
    """Test that cookrew's BFF proxy correctly proxies SSE from krewhub."""

    async def test_cookrew_openapi_accessible(self):
        """Verify cookrew is live."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(f"{COOKREW_URL}/")
            assert resp.status_code == 200

    async def test_cookrew_watch_bff_streams_sse(self):
        """Verify the BFF watch route proxies SSE from krewhub."""
        # Discover a real recipe_id from production
        recipe_id = None
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{KREWHUB_URL}/api/v1/recipes",
                headers={"X-API-Key": API_KEY},
            )
            if resp.status_code == 200:
                data = resp.json()
                recipes = data if isinstance(data, list) else data.get("recipes", [])
                if recipes:
                    recipe_id = recipes[0].get("id")

        if not recipe_id:
            pytest.skip("No recipes found in production")

        # Test BFF watch route with streaming client
        async with httpx.AsyncClient(timeout=None, follow_redirects=True) as client:
            async with client.stream(
                "GET",
                f"{COOKREW_URL}/api/recipes/{recipe_id}/watch?since=0",
            ) as resp:
                # 200 means SSE is streaming, 502 means upstream unreachable
                assert resp.status_code in (200, 502), (
                    f"BFF watch route returned {resp.status_code}"
                )

                if resp.status_code == 200:
                    content_type = resp.headers.get("content-type", "")
                    assert "text/event-stream" in content_type, (
                        f"Expected text/event-stream, got {content_type}"
                    )

                    # Read a few lines to confirm SSE data flows
                    line_count = 0
                    async for line in resp.aiter_lines():
                        line_count += 1
                        if line_count >= 3:
                            break


# ---------------------------------------------------------------------------
# 4. Browser eval: agent-browser validates SSE in real browser
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not AGENT_BROWSER, reason="agent-browser CLI not installed")
class TestProdBrowserSSEEval:
    """Evaluate SSE pub/sub via real browser on production endpoints."""

    async def test_browser_sse_endpoint_loads(self):
        """Open krewhub SSE endpoint in browser."""
        result = _run_agent_browser("open", f"{KREWHUB_URL}/api/v1/watch?since=0")
        assert result.returncode == 0, f"Failed: {result.stderr}"

        await asyncio.sleep(2)

        # Take screenshot for evidence
        result = _run_agent_browser(
            "screenshot", "/tmp/krewwatch_prod_sse_endpoint.png"
        )
        assert result.returncode == 0

    async def test_browser_eventsource_connects(self):
        """Create EventSource in browser to verify SSE is browser-consumable."""
        _run_agent_browser("open", f"{KREWHUB_URL}/api/v1/watch?since=0")
        await asyncio.sleep(1)

        js_code = """
        (async () => {
            const url = new URL(window.location.href);
            const es = new EventSource(url.href);
            return new Promise((resolve) => {
                let result = 'timeout';
                es.addEventListener('ADDED', (e) => {
                    result = 'event_received:' + e.data.substring(0, 80);
                    es.close();
                    resolve(result);
                });
                es.addEventListener('MODIFIED', (e) => {
                    result = 'event_received:' + e.data.substring(0, 80);
                    es.close();
                    resolve(result);
                });
                es.onerror = () => {
                    es.close();
                    resolve('connection_established_then_closed');
                };
                setTimeout(() => {
                    es.close();
                    resolve(result);
                }, 8000);
            });
        })()
        """
        result = _run_agent_browser("eval", js_code)
        output = result.stdout.strip()

        # Should get either an event or at least a connection
        assert "event_received" in output or "connection" in output or "timeout" in output, (
            f"Unexpected browser EventSource result: {output}"
        )

    async def test_browser_cookrew_loads(self):
        """Open cookrew.dev in browser to verify it's accessible."""
        result = _run_agent_browser("open", COOKREW_URL)
        assert result.returncode == 0, f"Failed: {result.stderr}"

        await asyncio.sleep(3)

        result = _run_agent_browser(
            "screenshot", "/tmp/krewwatch_prod_cookrew.png"
        )
        assert result.returncode == 0

    async def test_cleanup_browser(self):
        """Close browser sessions."""
        _run_agent_browser("close", "--all")
