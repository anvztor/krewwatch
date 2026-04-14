# krewwatch

Standalone SSE watch clients for the Krew ecosystem. Extracted from krewcli to enable independent testing and reuse.

## Package Structure

- `src/krewwatch/sse_watcher.py` — A2A invocation watcher (polling + SSE dual-path)
- `src/krewwatch/watch_client.py` — Task resource change watcher (SSE only)

## Dependencies

- Runtime: `httpx` only (no krewcli internals)
- Dev: `pytest`, `pytest-asyncio`, `starlette`, `uvicorn`

## Testing

```bash
# Unit tests (self-contained, no external deps)
uv run pytest tests/test_sse_watcher.py tests/test_watch_client.py -v

# E2E tests (requires krewhub at ../../krewhub)
uv run pytest tests/test_e2e_sse.py -v

# Browser evaluation (requires agent-browser CLI)
uv run pytest tests/test_eval_browser.py -v

# Production SSE pub/sub evaluation (requires internet + hub.cookrew.dev)
uv run pytest tests/test_eval_prod_sse.py -v

# All tests with coverage
uv run pytest --cov=krewwatch --cov-report=term-missing
```

## Evaluation Rules

These rules MUST be checked before merging changes to krewwatch:

### Unit & Integration (Local)

1. **All unit tests pass** — `uv run pytest tests/test_sse_watcher.py tests/test_watch_client.py`. Zero failures.
2. **Coverage >= 80%** — Run with `--cov=krewwatch`.
3. **SSEWatcher dedup works** — Same `invocation_id` never triggers `on_invocation` twice.
4. **WatchClient reconnection** — Must track `last_seq` and reconnect with `?since={seq}`.
5. **No krewcli imports** — `grep -r "from krewcli" src/krewwatch/` must find nothing.
6. **Backward compat** — `from krewcli.watch import WatchClient` must still work via shim.
7. **E2E lifecycle** — `test_e2e_sse.py` passes against real krewhub subprocess.

### Browser Evaluation (agent-browser CLI)

8. **SSE browser-consumable** — `test_eval_browser.py` confirms SSE works in real browser.
9. **EventSource connects** — Browser `new EventSource()` receives events from krewhub.
10. **Screenshot evidence** — Captured at `/tmp/sse_eval.png`.

### Production SSE Pub/Sub (hub.cookrew.dev)

11. **SSE stream is live** — `GET /api/v1/watch` returns `text/event-stream` with real events.
12. **Replay works** — `?since=0` replays historical events in sequence order.
13. **Resource filtering** — `?resource_type=task` returns only task events.
14. **Pub → Sub verified** — Agent heartbeat POST → event appears on SSE stream within seconds.
15. **cookrew BFF proxies SSE** — `GET /api/recipes/{id}/watch` on cookrew.dev returns `text/event-stream` from krewhub.
16. **Browser EventSource on prod** — `agent-browser eval` creates EventSource on hub.cookrew.dev and receives events.

### Full Pub/Sub Chain

The complete chain that must work end-to-end:

```
Agent → POST /api/v1/agents/heartbeat → krewhub stores event
                                          ↓
                                    WatchService.publish()
                                          ↓
                          GET /api/v1/watch (SSE stream)
                                    ↓              ↓
                        SSEWatcher (krewcli)   cookrew BFF proxy
                                                    ↓
                                            browser EventSource
                                                    ↓
                                              useWatch hook
                                                    ↓
                                           WorkspaceScreen reload
```

## Architecture Notes

- **SSEWatcher** uses dual-path delivery: periodic polling (primary/reliable) + SSE stream (secondary/fast). Both feed into a shared dedup handler.
- **WatchClient** is SSE-only with automatic reconnection and sequence replay.
- Auth differs: SSEWatcher uses Bearer JWT, WatchClient uses X-API-Key header.
- Both are stateless beyond sequence tracking — no database, no file I/O.
- **Production endpoints**: `hub.cookrew.dev` (krewhub), `cookrew.dev` (frontend BFF).
