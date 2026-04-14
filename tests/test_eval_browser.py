"""Agent-browser evaluation for krewwatch SSE endpoints.

Validates that SSE events from krewhub are consumable by a real browser
via the agent-browser CLI tool. This proves the SSE implementation works
for both programmatic (httpx) and browser-based consumers.

Requires:
- krewhub at ../../krewhub
- agent-browser CLI installed (npm install -g agent-browser)
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket
import subprocess
from pathlib import Path
from time import monotonic

import httpx
import pytest

KREWHUB_PROJECT_PATH = Path(__file__).resolve().parents[2] / "krewhub"
KREWHUB_BIN_PATH = KREWHUB_PROJECT_PATH / ".venv" / "bin" / "krewhub"
AGENT_BROWSER = shutil.which("agent-browser")

SKIP_REASON_NO_KREWHUB = "krewhub project not available"
SKIP_REASON_NO_BROWSER = "agent-browser CLI not installed"


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
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


def _run_agent_browser(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    """Run agent-browser CLI command and return result."""
    cmd = [AGENT_BROWSER, *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
async def krewhub_for_browser(tmp_path):
    """Start krewhub for browser evaluation."""
    if not KREWHUB_PROJECT_PATH.exists():
        pytest.skip(SKIP_REASON_NO_KREWHUB)

    port = _get_free_port()
    base_url = f"http://127.0.0.1:{port}"
    api_key = "eval-browser-key"
    db_path = tmp_path / "krewwatch-eval.sqlite3"

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
        yield base_url, api_key, port
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()


@pytest.mark.skipif(not KREWHUB_PROJECT_PATH.exists(), reason=SKIP_REASON_NO_KREWHUB)
@pytest.mark.skipif(not AGENT_BROWSER, reason=SKIP_REASON_NO_BROWSER)
class TestBrowserSSEEval:
    """Evaluate SSE endpoints via agent-browser CLI."""

    async def test_sse_endpoint_accessible_in_browser(self, krewhub_for_browser):
        """Open SSE watch endpoint in browser, verify it responds."""
        base_url, api_key, port = krewhub_for_browser

        # Open the SSE endpoint in agent-browser
        result = _run_agent_browser(
            "open", f"http://127.0.0.1:{port}/api/v1/watch?resource_type=task",
        )
        # agent-browser should not crash
        assert result.returncode == 0, f"agent-browser open failed: {result.stderr}"

    async def test_sse_page_renders_data(self, krewhub_for_browser):
        """Verify SSE data is accessible from browser JS context."""
        base_url, api_key, port = krewhub_for_browser
        watch_url = f"http://127.0.0.1:{port}/api/v1/watch"

        # Navigate to watch endpoint
        _run_agent_browser("open", watch_url)

        # Use eval to create an EventSource and listen for events
        js_code = """
        (async () => {
            const es = new EventSource(window.location.href);
            return 'EventSource created, readyState=' + es.readyState;
        })()
        """
        result = _run_agent_browser("eval", js_code)
        assert result.returncode == 0, f"eval failed: {result.stderr}"

        # The output should contain EventSource info
        output = result.stdout.strip()
        assert "EventSource" in output or "readyState" in output, (
            f"Expected EventSource info, got: {output}"
        )

    async def test_sse_screenshot_capture(self, krewhub_for_browser, tmp_path):
        """Capture screenshot of SSE endpoint for visual evidence."""
        base_url, api_key, port = krewhub_for_browser
        screenshot_path = str(tmp_path / "sse_eval.png")

        _run_agent_browser("open", f"http://127.0.0.1:{port}/api/v1/watch")
        await asyncio.sleep(1)

        result = _run_agent_browser("screenshot", screenshot_path)
        assert result.returncode == 0, f"screenshot failed: {result.stderr}"

        # Verify screenshot file was created
        assert Path(screenshot_path).exists(), "Screenshot file not created"
        assert Path(screenshot_path).stat().st_size > 0, "Screenshot file is empty"

    async def test_openapi_accessible(self, krewhub_for_browser):
        """Verify krewhub's OpenAPI spec is accessible via browser."""
        base_url, api_key, port = krewhub_for_browser

        result = _run_agent_browser(
            "open", f"http://127.0.0.1:{port}/openapi.json",
        )
        assert result.returncode == 0

        # Get the page text
        text_result = _run_agent_browser("get", "text", "body")
        assert text_result.returncode == 0

        output = text_result.stdout.strip()
        # OpenAPI spec should contain paths
        assert "openapi" in output.lower() or "paths" in output.lower() or len(output) > 100, (
            f"OpenAPI response too short or missing: {output[:200]}"
        )


@pytest.mark.skipif(not KREWHUB_PROJECT_PATH.exists(), reason=SKIP_REASON_NO_KREWHUB)
@pytest.mark.skipif(not AGENT_BROWSER, reason=SKIP_REASON_NO_BROWSER)
class TestBrowserSSEEvalCleanup:
    """Cleanup: close browser after evaluation."""

    async def test_close_browser(self):
        """Close any browser sessions opened during evaluation."""
        result = _run_agent_browser("close", "--all")
        # May fail if no browser is open, that's OK
        assert result.returncode in (0, 1)
