"""Shared fixtures for krewwatch tests.

Provides a fake SSE server (Starlette + uvicorn) that serves:
- GET  /api/v1/watch           — SSE stream of events
- GET  /a2a/{owner}/{agent}/pending — returns pending invocations
- POST /a2a/respond            — captures A2A responses
"""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any, AsyncIterator

import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route


class FakeHub:
    """In-memory fake krewhub for testing SSE watch clients."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.pending_invocations: dict[str, list[dict]] = {}  # key: "{owner}/{agent}"
        self.responses: list[dict] = []
        self._waiters: list[asyncio.Event] = []
        self._seq = 0
        # When set, requests whose Authorization header doesn't end with this
        # token receive a 401. Lets tests exercise refresh-on-401 behavior.
        self.required_token: str | None = None
        self.unauthorized_hits: int = 0

    def push_event(self, event: dict[str, Any]) -> None:
        self._seq += 1
        event.setdefault("seq", self._seq)
        self.events.append(event)
        for w in self._waiters:
            w.set()

    def add_pending(self, owner: str, agent: str, invocation: dict) -> None:
        key = f"{owner}/{agent}"
        self.pending_invocations.setdefault(key, []).append(invocation)

    def _check_auth(self, request: Request) -> JSONResponse | None:
        if self.required_token is None:
            return None
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {self.required_token}":
            self.unauthorized_hits += 1
            return JSONResponse({"detail": "Missing or invalid credentials"}, status_code=401)
        return None

    def build_app(self) -> Starlette:
        hub = self

        async def watch_sse(request: Request):
            err = hub._check_auth(request)
            if err is not None:
                return err
            since = int(request.query_params.get("since", "0"))

            async def stream() -> AsyncIterator[str]:
                idx = 0
                while True:
                    while idx < len(hub.events):
                        ev = hub.events[idx]
                        idx += 1
                        if ev.get("seq", 0) <= since:
                            continue
                        yield f"event: a2a_invocation\ndata: {json.dumps(ev)}\n\n"
                    waiter = asyncio.Event()
                    hub._waiters.append(waiter)
                    try:
                        await asyncio.wait_for(waiter.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        yield "event: ping\ndata: {}\n\n"
                    finally:
                        if waiter in hub._waiters:
                            hub._waiters.remove(waiter)

            return StreamingResponse(stream(), media_type="text/event-stream")

        async def pending(request: Request) -> JSONResponse:
            err = hub._check_auth(request)
            if err is not None:
                return err
            owner = request.path_params["owner"]
            agent = request.path_params["agent"]
            key = f"{owner}/{agent}"
            items = hub.pending_invocations.pop(key, [])
            return JSONResponse(items)

        async def respond(request: Request) -> JSONResponse:
            body = await request.json()
            hub.responses.append(body)
            return JSONResponse({"ok": True})

        return Starlette(
            routes=[
                Route("/api/v1/watch", watch_sse),
                Route("/a2a/{owner}/{agent}/pending", pending),
                Route("/a2a/respond", respond, methods=["POST"]),
            ],
        )


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def fake_hub():
    """Start a FakeHub server on a random port. Yields (hub, base_url)."""
    hub = FakeHub()
    app = hub.build_app()
    port = _get_free_port()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    task = asyncio.create_task(server.serve())

    # Wait for server to be ready
    import httpx

    for _ in range(50):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/a2a/test/test/pending")
                if resp.status_code == 200:
                    break
        except Exception:
            pass
        await asyncio.sleep(0.1)

    yield hub, f"http://127.0.0.1:{port}"

    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        task.cancel()
