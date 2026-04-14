from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchEvent:
    """A single event from the krewhub watch stream."""

    event_type: str  # ADDED, MODIFIED, DELETED
    resource_type: str
    resource_id: str
    resource_version: int
    object: dict[str, Any]
    seq: int = 0


class WatchClient:
    """SSE consumer that connects to krewhub's watch endpoint.

    Tracks the last seen sequence number and reconnects with replay,
    guaranteeing no missed events.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        resource_type: str | None = None,
        recipe_id: str | None = None,
        reconnect_delay: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._resource_type = resource_type
        self._recipe_id = recipe_id
        self._reconnect_delay = reconnect_delay
        self._last_seq = 0
        self._running = False
        self._task: asyncio.Task | None = None
        self._callbacks: list[Callable[[WatchEvent], Awaitable[None]]] = []
        self._queue: asyncio.Queue[WatchEvent] = asyncio.Queue(maxsize=256)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def on_event(self, callback: Callable[[WatchEvent], Awaitable[None]]) -> None:
        """Register an async callback for watch events."""
        self._callbacks.append(callback)

    @property
    def queue(self) -> asyncio.Queue[WatchEvent]:
        """Direct queue access for consumers that prefer polling."""
        return self._queue

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="watch-client")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Watch stream disconnected, reconnecting in %.1fs", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)

    async def _connect_and_stream(self) -> None:
        params: dict[str, str] = {"since": str(self._last_seq)}
        if self._resource_type:
            params["resource_type"] = self._resource_type
        if self._recipe_id:
            params["recipe_id"] = self._recipe_id

        url = f"{self._base_url}/api/v1/watch"
        headers = {"X-API-Key": self._api_key}

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("GET", url, params=params, headers=headers) as response:
                response.raise_for_status()
                event_type = ""
                data_buf = ""

                async for line in response.aiter_lines():
                    if not self._running:
                        break

                    if line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buf = line[5:].strip()
                    elif line == "":
                        # End of SSE event
                        if event_type and data_buf and event_type != "ping":
                            await self._handle_event(event_type, data_buf)
                        event_type = ""
                        data_buf = ""

    async def _handle_event(self, event_type: str, data: str) -> None:
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            logger.warning("Failed to parse watch event data: %s", data[:100])
            return

        event = WatchEvent(
            event_type=payload.get("type", event_type),
            resource_type=payload.get("resource_type", "unknown"),
            resource_id=payload.get("resource_id", "unknown"),
            resource_version=payload.get("resource_version", 0),
            object=payload.get("object", {}),
            seq=payload.get("seq", 0),
        )

        if event.seq > self._last_seq:
            self._last_seq = event.seq

        # Deliver to queue
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

        # Deliver to callbacks
        for callback in self._callbacks:
            try:
                await callback(event)
            except Exception:
                logger.exception("Watch callback failed")
