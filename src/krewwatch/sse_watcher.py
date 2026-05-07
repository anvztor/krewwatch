"""SSE Watcher — polls krewhub for A2A invocations with SSE live backup.

Primary: periodic poll of /a2a/{owner}/{agent}/pending (reliable, simple)
Secondary: SSE watch stream for instant delivery (best-effort)

Both paths feed into the same _handle_event → _on_invocation pipeline.
Deduplication via _processed set prevents double-execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import httpx

logger = logging.getLogger(__name__)


class SSEWatcher:
    """Watch krewhub for A2A invocations via polling + SSE."""

    def __init__(
        self,
        krewhub_url: str,
        jwt_token: str,
        owner: str,
        agent_names: list[str],
        on_invocation: Callable[[dict], Awaitable[dict | None]],
        poll_interval: float = 5.0,
        token_reloader: Callable[[], str | None] | None = None,
        max_concurrent_invocations: int = 5,
    ):
        """Args:
            token_reloader: optional zero-arg callable invoked on 401 to
                fetch a fresh JWT (e.g. from a disk cache). Returning None
                or the same token is treated as "no refresh available" and
                the request is abandoned. Long-running daemons use this to
                recover from token rotation without a restart.
            max_concurrent_invocations: cap on parallel ``on_invocation``
                calls. Before this was added the watcher dispatched
                invocations strictly serially in ``_poll_once`` /
                ``_sse_once``, so a 30-minute task would block every
                subsequent invocation for 30 minutes — leaving freshly
                dispatched tasks visibly stuck in `claimed` while the
                hub still held them in the pending queue.
        """
        self._krewhub_url = krewhub_url
        self._jwt_token = jwt_token
        self._owner = owner
        self._agent_names = set(agent_names)
        self._on_invocation = on_invocation
        self._poll_interval = poll_interval
        self._token_reloader = token_reloader
        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._sse_task: asyncio.Task | None = None
        self._processed: set[str] = set()  # dedup invocation ids
        self._last_seq = 0
        # Bounded concurrency for invocation dispatch. The semaphore
        # guards the actual work; _handle_event itself stays cheap so
        # poll/sse loops keep moving.
        self._concurrency = asyncio.Semaphore(max_concurrent_invocations)
        self._inflight: set[asyncio.Task] = set()

    def _maybe_refresh_token(self) -> bool:
        """Reload the JWT from the registered reloader. Returns True on refresh."""
        if self._token_reloader is None:
            return False
        fresh = self._token_reloader()
        if not fresh or fresh == self._jwt_token:
            return False
        logger.warning("krewhub returned 401; JWT refreshed from reloader")
        self._jwt_token = fresh
        return True

    def start(self) -> None:
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop(), name="sse-poll")
        self._sse_task = asyncio.create_task(self._sse_loop(), name="sse-stream")
        logger.info("SSE watcher started for %s agents: %s", self._owner, self._agent_names)

    async def stop(self) -> None:
        self._running = False
        for task in [self._poll_task, self._sse_task]:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Drain in-flight invocation handlers so we don't leave
        # _on_invocation futures dangling on shutdown.
        if self._inflight:
            logger.info("SSE watcher draining %d in-flight invocations", len(self._inflight))
            await asyncio.gather(*self._inflight, return_exceptions=True)
        logger.info("SSE watcher stopped")

    # ------------------------------------------------------------------
    # Primary: Periodic polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Poll error: %s", e)
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        async with httpx.AsyncClient(timeout=15) as client:
            for agent_name in self._agent_names:
                try:
                    resp = await client.get(
                        f"{self._krewhub_url}/a2a/{self._owner}/{agent_name}/pending",
                        headers={"Authorization": f"Bearer {self._jwt_token}"},
                    )
                    if resp.status_code == 401 and self._maybe_refresh_token():
                        resp = await client.get(
                            f"{self._krewhub_url}/a2a/{self._owner}/{agent_name}/pending",
                            headers={"Authorization": f"Bearer {self._jwt_token}"},
                        )
                    if resp.status_code != 200:
                        continue

                    pending = resp.json()
                    if pending:
                        logger.info("Poll: %d pending for %s/%s", len(pending), self._owner, agent_name)
                    for inv in pending:
                        await self._handle_event({
                            "id": inv["invocation_id"],
                            "owner": self._owner,
                            "agent_name": agent_name,
                            "method": inv["method"],
                            "params": inv["params"],
                            "message": json.dumps(inv.get("params", {})),
                        })
                except Exception as e:
                    logger.debug("Poll %s/%s failed: %s", self._owner, agent_name, e)

    # Expose for one-shot startup call
    async def poll_pending(self) -> None:
        await self._poll_once()

    async def wait_inflight(self) -> None:
        """Block until every spawned invocation handler has completed.

        Used by tests + graceful-shutdown paths to assert "no work in
        flight" before checking response state. Production runs leave
        invocations running concurrently — this is a deliberate join
        point, not an opportunistic flush.
        """
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)

    # ------------------------------------------------------------------
    # Secondary: SSE live stream (best-effort, faster delivery)
    # ------------------------------------------------------------------

    async def _sse_loop(self) -> None:
        while self._running:
            try:
                await self._sse_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("SSE connection lost: %s, reconnecting in 5s", e)
                await asyncio.sleep(5)

    async def _sse_once(self) -> None:
        url = f"{self._krewhub_url}/api/v1/watch?since={self._last_seq}"

        async with httpx.AsyncClient(timeout=None) as client:
            headers = {"Authorization": f"Bearer {self._jwt_token}"}
            async with client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 401 and self._maybe_refresh_token():
                    await asyncio.sleep(1)
                    return
                if resp.status_code != 200:
                    logger.debug("SSE connect failed: %d", resp.status_code)
                    await asyncio.sleep(10)
                    return

                logger.info("SSE connected (since=%d)", self._last_seq)
                async for line in resp.aiter_lines():
                    if not self._running:
                        break
                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if not data_str:
                        continue

                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    seq = event.get("seq", 0)
                    if seq > self._last_seq:
                        self._last_seq = seq

                    if event.get("resource_type") != "a2a_invocation":
                        continue

                    payload = event.get("object", event)
                    await self._handle_event(payload)

    # ------------------------------------------------------------------
    # Shared handler (deduped)
    # ------------------------------------------------------------------

    async def _handle_event(self, payload: dict) -> None:
        """Validate + dedup the event, then spawn the dispatch as a
        fire-and-forget task. Returns quickly so the poll/sse loops
        keep moving even when individual invocations run for many
        minutes."""
        agent_name = payload.get("agent_name")
        owner = payload.get("owner")
        invocation_id = payload.get("id")

        if not invocation_id:
            return
        if owner != self._owner or agent_name not in self._agent_names:
            return
        if invocation_id in self._processed:
            return

        # Mark processed BEFORE spawning the handler so a near-
        # simultaneous SSE/poll arrival of the same invocation
        # doesn't double-fire while the handler awaits the semaphore.
        self._processed.add(invocation_id)
        # Cap dedup set size
        if len(self._processed) > 1000:
            self._processed = set(list(self._processed)[-500:])

        logger.info("A2A invocation: %s for %s/%s", invocation_id, owner, agent_name)

        # Fire-and-forget the dispatch under a bounded semaphore so up
        # to ``max_concurrent_invocations`` runs in parallel. Without
        # this the poll loop blocked on the FULL execution of each
        # invocation — a long task held the queue closed for the
        # duration of its run, leaving subsequent tasks stuck in
        # `claimed` from the hub side.
        task = asyncio.create_task(
            self._run_invocation(payload, invocation_id),
            name=f"sse-invocation:{invocation_id}",
        )
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    async def _run_invocation(self, payload: dict, invocation_id: str) -> None:
        """The actual dispatch + respond. Bounded by ``self._concurrency``."""
        async with self._concurrency:
            try:
                result = await self._on_invocation(payload)

                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{self._krewhub_url}/a2a/respond",
                        json={"invocation_id": invocation_id, "result": result},
                        headers={"Authorization": f"Bearer {self._jwt_token}"},
                    )
                    if resp.status_code == 200:
                        logger.info("A2A response posted for %s", invocation_id)
                    else:
                        logger.error(
                            "A2A response failed: %d %s",
                            resp.status_code, resp.text[:200],
                        )

            except Exception as e:
                logger.exception("A2A invocation %s failed: %s", invocation_id, e)
                try:
                    async with httpx.AsyncClient(timeout=10) as client:
                        await client.post(
                            f"{self._krewhub_url}/a2a/respond",
                            json={"invocation_id": invocation_id, "error": str(e)},
                            headers={"Authorization": f"Bearer {self._jwt_token}"},
                        )
                except Exception:
                    pass
