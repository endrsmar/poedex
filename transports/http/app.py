"""FastAPI over the method registry and the event bus.

Three endpoints and a static mount, and no knowledge of what a verdict is:

* ``POST /api/call/{method}`` — the method registry, one call per request.
* ``GET  /api/events`` — the runtime event bus, bridged to SSE.
* ``GET  /api/meta`` — modules, their state, and the callable names.
* ``/`` — the built SPA, with an index fallback so a client-side route reloads.

## Why the host guard exists

This server exposes an account's inventory. It binds to 127.0.0.1 (see
``server.py``), which stops another machine reaching it — but *not* a web page in
the user's own browser: any site can `POST http://127.0.0.1:7331/...`, and with DNS
rebinding it can reach it under its own origin. So:

* **No CORS headers, ever.** Not permissive ones, not `*`. Their absence is what
  makes the browser refuse to hand a cross-origin page the *response*.
* **`Origin` must be absent or loopback.** A page at `https://example.com` sending a
  no-cors POST would still reach the handler and still cause a fetch of the
  player's stash; this rejects it before dispatch.
* **`Host` must be loopback.** That is the DNS-rebinding case: the packet arrives on
  127.0.0.1 but carries `Host: evil.test`. A real local client never does that.

None of this is defence in depth for a public service. It is the minimum that makes
"a local port that reads your account" not a hole for every tab you have open.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from runtime.events import Event
from runtime.log import get_logger
from runtime.registry import Registry
from transports.dispatch import call_method, server_meta

__all__ = ["LOOPBACK_HOST", "create_app"]

_log = get_logger("transport.http")

LOOPBACK_HOST = "127.0.0.1"
VERSION = "0.1.0"

#: Hostnames a `Host:` or `Origin:` header may legitimately carry for a server that
#: is only listening on the loopback interface.
LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "[::1]", "::1", "0:0:0:0:0:0:0:1"})

#: How many events may pile up for one slow SSE client before it is dropped. The
#: bus must never block on a browser tab that stopped reading.
EVENT_QUEUE_SIZE = 256

#: Seconds between SSE keep-alive comments. Proxies and some browsers close an idle
#: event stream, and a bag screen that silently stops receiving updates looks
#: exactly like a bag that stopped changing.
HEARTBEAT_SECONDS = 15.0


def create_app(
    registry: Registry,
    *,
    static_dir: Path | str | None = None,
    version: str = VERSION,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> FastAPI:
    app = FastAPI(
        title="PoEDex",
        version=version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.registry = registry

    @app.middleware("http")
    async def guard_local_only(request: Request, call_next: Any) -> Response:
        problem = _reject_reason(request)
        if problem is not None:
            _log.warning("refused a request: %s", problem)
            return JSONResponse(
                status_code=403,
                content={
                    "ok": False,
                    "error": {"kind": "ForbiddenOrigin", "message": problem, "retry_after": None},
                },
            )
        return await call_next(request)

    @app.get("/api/meta")
    async def meta() -> JSONResponse:
        return JSONResponse(server_meta(registry, version=version))

    @app.post("/api/call/{method}")
    async def call(method: str, request: Request) -> JSONResponse:
        params = await _read_params(request)
        if params is None:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": {
                        "kind": "BadRequest",
                        "message": "the body must be a JSON object of keyword arguments",
                        "retry_after": None,
                    },
                },
            )
        outcome = await call_method(registry, method, params, version=version)
        headers: dict[str, str] = {}
        if outcome.retry_after is not None:
            # The limiter's own number, so a client can count down against the same
            # figure the backend is enforcing rather than guessing.
            headers["Retry-After"] = str(int(outcome.retry_after) + 1)
        return JSONResponse(
            status_code=outcome.status,
            headers=headers,
            content={"ok": outcome.ok, "result": outcome.result, "error": outcome.error},
        )

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(registry, request, heartbeat=heartbeat),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    _mount_spa(app, static_dir)
    return app


# -- static ------------------------------------------------------------------


def _mount_spa(app: FastAPI, static_dir: Path | str | None) -> None:
    """Serve the built SPA, with an index fallback for client-side routes.

    Registered last so it can hold the catch-all without shadowing ``/api``. When
    there is no build the catch-all says so in one sentence, because "404 not found"
    on the root of your own tool is a much worse answer than "run pnpm build".
    """
    root = Path(static_dir) if static_dir else None
    index = root / "index.html" if root else None

    if root and root.is_dir():
        assets = root / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str) -> Response:
        if path.startswith("api/"):
            return JSONResponse(status_code=404, content={"ok": False, "error": None})
        if root and root.is_dir():
            candidate = (root / path).resolve()
            # Reject anything that escapes the build directory before touching it.
            if path and root.resolve() in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            if index and index.is_file():
                return FileResponse(index)
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "error": {
                    "kind": "NoBuild",
                    "message": (
                        "the web surface has not been built — run 'pnpm install && "
                        "pnpm build' from the repo root, then start 'poedex serve' "
                        "again. To iterate on the frontend instead, run 'pnpm dev' "
                        "in one terminal and 'poedex serve' in another: Vite serves "
                        "the UI on :5173 and proxies /api to this server."
                    ),
                    "retry_after": None,
                },
            },
        )


# -- request helpers ---------------------------------------------------------


async def _read_params(request: Request) -> dict[str, Any] | None:
    raw = await request.body()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        return None
    # Keyword arguments only. A positional call over a transport is a call that
    # breaks the day someone reorders a Python signature.
    return {str(key): value for key, value in parsed.items()}


def _reject_reason(request: Request) -> str | None:
    host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip().lower()
    if host and host not in LOOPBACK_NAMES:
        return f"Host header {host!r} is not loopback; refusing (DNS rebinding)"
    origin = request.headers.get("origin")
    if origin:
        hostname = origin.split("//", 1)[-1].rsplit(":", 1)[0].strip().lower()
        if hostname not in LOOPBACK_NAMES:
            return f"cross-origin request from {origin!r} refused"
    return None


# -- SSE ---------------------------------------------------------------------


async def _event_stream(
    registry: Registry, request: Request, *, heartbeat: float
) -> AsyncIterator[bytes]:
    """Bridge the runtime event bus onto one SSE connection.

    The bus delivers to handlers with ``await``, so this handler does the one thing
    a bus handler must do: put on a bounded queue and return. A browser that stops
    reading fills its queue and gets dropped; it does not get to slow down
    ``sync_complete`` for the module that emitted it.
    """
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)

    def receive(event: Event) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            _log.warning("SSE client is not keeping up; dropping %s", event.topic)

    unsubscribe = registry.events.subscribe("*", receive)
    try:
        # An immediate comment flushes headers, so `EventSource.onopen` fires now
        # rather than on the first real event — which on a quiet bag is never.
        yield b": connected\n\n"
        while True:
            if await request.is_disconnected():
                return
            try:
                event = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except TimeoutError:
                yield b": ping\n\n"
                continue
            yield _sse(event)
    finally:
        unsubscribe()


def _sse(event: Event) -> bytes:
    payload = json.dumps(
        {
            "topic": event.topic,
            "payload": dict(event.payload),
            "source": event.source,
            "at": event.at,
        },
        default=str,
    )
    # The topic goes in `event:` as well as in the body: `event:` lets a future
    # client use `addEventListener(topic)`, and the body keeps the current one
    # working with a single `message` handler.
    return f"event: {event.topic}\ndata: {payload}\n\n".encode()
