"""`poedex serve` — the first usable surface.

Starts the runtime, then the HTTP transport, and prints the one URL that matters.
The registry is already running by the time the socket opens, so the first request
does not race module startup — which on this project means it does not race the
poe.ninja table prefetch.
"""

from __future__ import annotations

import sys
from pathlib import Path

from runtime.registry import Registry
from transports.http.server import DEFAULT_PORT, NonLoopbackBindError, default_static_dir, serve


async def cmd_serve(
    registry: Registry,
    *,
    host: str,
    port: int,
    static_dir: str | None = None,
) -> int:
    root = Path(static_dir) if static_dir else default_static_dir()
    if not (root / "index.html").is_file():
        print(
            f"note:  no SPA build at {root} — the API is up but the page is not.\n"
            "       build it with:  pnpm install && pnpm build",
            file=sys.stderr,
        )
    print(f"poedex  http://{host}:{port}")
    print("        Ctrl-C to stop. Bound to loopback only; nothing else can reach it.")
    for module_id, info in registry.status().items():
        if info["state"] not in {"started", "registered"}:
            print(f"        {module_id}: {info['state']} — {info['reason'] or 'no reason given'}")
    try:
        await serve(registry, host=host, port=port, static_dir=root)
    except NonLoopbackBindError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    return 0


__all__ = ["DEFAULT_PORT", "cmd_serve"]
