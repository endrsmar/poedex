"""The Decky Loader entry point. Glue, and one landmine that had to be disarmed.

Decky forks a process per plugin and loads this file by path
(`spec_from_file_location` in the loader's `sandboxed_plugin.py`), then looks for a
class called `Plugin`. Frontend-callable methods are `async def` and must not start
with an underscore; `_main`, `_unload` and `_migration` are the reserved lifecycle
hooks.

Everything of substance is in `transports/decky/backend.py`, which never imports
`decky` and is therefore testable on a machine with no Steam Deck —
`tests/test_transport_decky.py`. What is left here is the import path fix, the
logging fix, and five one-line methods.

## The import path fix, and why it is first

Decky's loader **appends** the plugin's `py_modules` to `sys.path`:

    # backend/decky_loader/plugin/sandboxed_plugin.py
    sys.path.append(path.join(environ["DECKY_PLUGIN_DIR"], "py_modules"))

Appending puts it last, behind PyInstaller's `_MEIPASS` entries — and the frozen
loader bundles `setuptools`, whose `_vendor/typing_extensions.py` therefore
**shadows the one pydantic needs**. The observed failure is
`ImportError: cannot import name 'Sentinel' from 'typing_extensions'`, pointing at a
file inside the loader's own temp directory. Verified against the real
`PluginLoader` binary in `ghcr.io/steamdeckhomebrew/holo-base`.

So `py_modules` is prepended here and the stale module is dropped from
`sys.modules`, before anything third-party is imported. That is the entire reason
these lines are above the imports.

## The other verified fact this file depends on

The plugin process runs under the **frozen loader's own CPython 3.11**, not SteamOS's
`/usr/bin/python3` (3.13 since SteamOS 3.7). `pydantic-core` publishes no `abi3`
wheel, so `py_modules/` must carry the `cp311` build —
`scripts/build_plugin.py` is what puts it there, and `_check_vendored()` below is
what says so out loud rather than dying on an ImportError nobody can read.
"""

from __future__ import annotations

import os
import sys

# --- must run before any third-party import; see the module docstring ---------
_PLUGIN_DIR = os.environ.get("DECKY_PLUGIN_DIR") or os.path.dirname(os.path.abspath(__file__))
_PY_MODULES = os.path.join(_PLUGIN_DIR, "py_modules")
if _PY_MODULES not in sys.path:
    sys.path.insert(0, _PY_MODULES)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)
for _name in [key for key in list(sys.modules) if key.split(".")[0] == "typing_extensions"]:
    del sys.modules[_name]
# ------------------------------------------------------------------------------

import logging  # noqa: E402
from typing import Any  # noqa: E402

import decky  # noqa: E402  - only exists inside the plugin host

from transports.decky import DeckyBackend, install_decky_logging  # noqa: E402

# `decky.logger` already reconfigured the root logger with `force=True` by the time
# this file is imported, so the redacting filter and the httpx silence have to be
# reinstalled on top of it. Doing it at import time rather than in `_main` means a
# failure during module import is redacted too.
install_decky_logging(logging.getLogger())

_backend = DeckyBackend(emit=decky.emit, modules_root=os.path.join(_PLUGIN_DIR, "modules"))


def _check_vendored() -> str | None:
    """Name the missing dependency instead of dying on a traceback nobody reads.

    The one realistic failure is a `pydantic-core` built for the wrong CPython: it is
    a compiled extension with no `abi3` wheel, so a `.so` tagged `cp313` is simply
    not *found* by a 3.11 interpreter and the error is `ModuleNotFoundError`, which
    reads as "you forgot to vendor it" rather than "you vendored the wrong one".
    """
    try:
        import httpx  # noqa: F401
        import pydantic  # noqa: F401
    except Exception as exc:
        return (
            f"PoEDex cannot start: {type(exc).__name__}: {exc}. "
            f"py_modules/ is built for CPython "
            f"{sys.version_info.major}.{sys.version_info.minor}; rebuild it with "
            f"`python scripts/build_plugin.py` and reinstall."
        )
    return None


class Plugin:
    """What Decky Loader instantiates.

    Every public coroutine here is reachable from the panel through
    `callable('<name>')`. There are five, and only one of them takes a method name —
    dispatch lives in `transports/dispatch.py` and is shared with the HTTP transport
    rather than written twice.
    """

    # -- frontend-callable -----------------------------------------------------

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke one registered method.

        Decky's `callable(route)(...args)` passes **positional** arguments to the
        same-named coroutine, which does not fit a registry of namespaced methods with
        keyword arguments. So there is one door, and `DeckyTransport.call()` on the
        frontend is the other side of it.
        """
        return await _backend.call(method, params or {})

    async def meta(self) -> dict[str, Any]:
        """Modules, their state, their reason, and every callable name."""
        return _backend.meta()

    async def latest(self, topic: str | None = None) -> dict[str, Any]:
        """The most recent payload per topic, for a panel that just mounted.

        Panel content is unmounted whenever the QAM closes, so a screen cannot rely on
        having been subscribed when an event fired.
        """
        return _backend.latest(topic)

    async def health(self) -> dict[str, Any]:
        """Enough to tell "starting" from "broken" without reading the log."""
        return {
            "started": _backend.started,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "resumes": _backend.resumes,
            "problem": _check_vendored(),
        }

    # -- lifecycle -------------------------------------------------------------

    async def _main(self) -> None:
        """Scheduled by the loader and **not awaited**.

        The backend process is independent of the panel: it keeps running when the
        QAM closes, across game launch and exit, and across a Steam UI reload
        (research-notes §5). So starting the registry here — once — is the intended
        shape, and the panel opens against something already warm.
        """
        problem = _check_vendored()
        if problem:
            decky.logger.error(problem)
            # Told rather than raised: a raise here is a plugin that shows up in the
            # list and does nothing, with the reason only in a log file the player
            # would have to leave gaming mode to read.
            await decky.emit("poedex", {"topic": "backend.broken", "payload": {"detail": problem}})
            return
        try:
            await _backend.start()
        except Exception as exc:
            decky.logger.exception("poedex failed to start")
            await decky.emit(
                "poedex",
                {
                    "topic": "backend.broken",
                    "payload": {"detail": f"{type(exc).__name__}: {exc}"},
                },
            )

    async def _unload(self) -> None:
        """Decky SIGKILLs five seconds after SIGTERM, so this finishes on time.

        The budget is inside `DeckyBackend.shutdown`, which stops waiting on the
        registry after three seconds. The one thing that must always happen is the
        pairing socket closing, and `credentials.stop()` does that first.
        """
        await _backend.shutdown()

    async def _uninstall(self) -> None:
        await _backend.shutdown()

    async def _migration(self) -> None:
        """Nothing to migrate yet.

        Deliberately present and empty: the settings and cache directories come from
        `DECKY_PLUGIN_SETTINGS_DIR` / `DECKY_PLUGIN_RUNTIME_DIR`, which
        `runtime/storage.py` already reads, so there is no legacy path to move.
        """
        return None
