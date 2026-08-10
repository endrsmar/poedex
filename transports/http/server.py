"""Running the HTTP transport. 127.0.0.1 only, and not by convention.

The server exposes an account's whole backpack and, later, its stash. Binding it to
anything reachable from another machine is not a configuration choice this tool
offers, so :func:`assert_loopback` refuses rather than warns — a warning printed at
startup on a Deck in gaming mode is a warning nobody reads.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path

from runtime.registry import Registry
from transports.http.app import LOOPBACK_HOST, create_app

__all__ = ["DEFAULT_PORT", "LOOPBACK_HOST", "assert_loopback", "build", "serve"]

DEFAULT_PORT = 7331
"""IMPLEMENTATION-PLAN §6: 7331 avoids Decky's 1337."""


class NonLoopbackBindError(RuntimeError):
    """Someone asked this server to listen somewhere another machine can reach."""


def assert_loopback(host: str) -> str:
    """Return ``host`` if it is loopback; raise otherwise.

    ``0.0.0.0`` and ``::`` are the interesting cases: they are not *remote*
    addresses, they are "every interface", which is the mistake that puts a stash
    reader on a café's wifi.
    """
    name = (host or "").strip()
    if name.lower() in {"localhost", "localhost.localdomain"}:
        return name
    try:
        address = ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        raise NonLoopbackBindError(
            f"refusing to bind to {host!r}: PoEDex serves account data and listens "
            f"on {LOOPBACK_HOST} only"
        ) from None
    if not address.is_loopback:
        raise NonLoopbackBindError(
            f"refusing to bind to {host!r}: PoEDex serves account data and listens "
            f"on {LOOPBACK_HOST} only"
            + (
                " (0.0.0.0 means every interface, including the network you are on)"
                if address.is_unspecified
                else ""
            )
        )
    return name


def default_static_dir() -> Path:
    """Where ``pnpm build`` puts the SPA."""
    return Path(__file__).resolve().parents[2] / "surfaces" / "web" / "dist"


def build(
    registry: Registry,
    *,
    static_dir: Path | str | None = None,
):
    """The ASGI app, with the SPA build wired in if one exists."""
    root = Path(static_dir) if static_dir is not None else default_static_dir()
    return create_app(registry, static_dir=root)


async def serve(
    registry: Registry,
    *,
    host: str = LOOPBACK_HOST,
    port: int = DEFAULT_PORT,
    static_dir: Path | str | None = None,
    log_level: str = "warning",
) -> None:
    """Run until cancelled. The registry is started and stopped by the caller."""
    import uvicorn

    bind = assert_loopback(host)
    config = uvicorn.Config(
        build(registry, static_dir=static_dir),
        host=bind,
        port=port,
        log_level=log_level,
        access_log=False,
        # The credential never travels over this socket, but the redacting logger is
        # installed process-wide and uvicorn's own handlers must not undo it.
        log_config=None,
    )
    await uvicorn.Server(config).serve()
