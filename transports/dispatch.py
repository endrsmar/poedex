"""Method dispatch, shared by every transport.

The HTTP transport in Phase 5 and the Decky transport in Phase 7 differ in how
bytes arrive and in nothing else, so the parts that are *not* about bytes live
here: which built-in methods exist, how a failure becomes a structured error, and
what a transport is allowed to expose.

Two rules are enforced here rather than trusted:

* **The credential is not reachable.** ``credentials.session_id`` is not in that
  module's ``methods()``, so no name maps to it. :func:`exposed_methods` additionally
  refuses to serve any name on :data:`FORBIDDEN_METHODS`, which is a belt to that
  braces: if a future module ever registers a secret accessor, this transport does
  not serve it and ``tests/test_transport_http.py`` fails loudly.
* **Every message out is redacted.** Exceptions cross into a browser console and,
  on the Deck, into the plugin log. ``runtime.secrets.redact`` runs over every error
  string this module produces.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from runtime.errors import PoedexError, UnknownMethodError
from runtime.registry import Registry
from runtime.secrets import redact

__all__ = [
    "BUILTIN_PREFIX",
    "FORBIDDEN_METHODS",
    "DispatchResult",
    "call_method",
    "describe_error",
    "exposed_methods",
    "server_meta",
]

BUILTIN_PREFIX = "_server."
"""Transport-provided methods. The registry refuses a leading underscore on a
*module* method, which is exactly why this prefix is safe to reserve: no module can
collide with it."""

FORBIDDEN_METHODS = frozenset(
    {
        "credentials.session_id",
        "credentials.reveal",
        "net.get",
    }
)
"""Names a transport must never serve even if something registers them.

``net.get`` is on the list for a different reason from the other two: an
arbitrary-URL fetch reachable from a browser console is a credential-bearing proxy
for whoever can reach the port. `net`'s own ``methods()`` already omits it and says
so; this is the second lock."""


@dataclass(frozen=True)
class DispatchResult:
    ok: bool
    result: Any = None
    error: dict[str, Any] | None = None
    status: int = 200
    retry_after: float | None = None


def exposed_methods(registry: Registry) -> list[str]:
    """Every callable name a transport will serve, built-ins included."""
    return sorted(
        [name for name in registry.methods.names() if name not in FORBIDDEN_METHODS]
        + [f"{BUILTIN_PREFIX}meta"]
    )


def server_meta(registry: Registry, *, version: str, profile: str = "full") -> dict[str, Any]:
    """What a shell asks for before it mounts anything.

    Includes each module's ``state`` and its ``reason`` — plan §1.5 says a disabled
    dependency disables the dependent *with a stated reason surfaced in the UI*, and
    a UI cannot surface what the transport does not send.
    """
    modules = []
    for module_id, info in registry.status().items():
        modules.append(
            {
                "id": info.get("id", module_id),
                "name": info.get("name") or module_id,
                "kind": info.get("kind", "feature"),
                "state": info.get("state", "registered"),
                "requires": list(info.get("requires") or []),
                "reason": info.get("reason"),
                "methods": [
                    name
                    for name in registry.methods.for_module(module_id)
                    if name not in FORBIDDEN_METHODS
                ],
            }
        )
    return {
        "version": version,
        "profile": profile,
        "modules": modules,
        "methods": exposed_methods(registry),
    }


async def call_method(
    registry: Registry,
    method: str,
    params: Mapping[str, Any] | None = None,
    *,
    version: str = "0.0.0",
) -> DispatchResult:
    """Dispatch one call and turn any failure into a structured, redacted error."""
    kwargs = dict(params or {})

    if method.startswith(BUILTIN_PREFIX):
        if method == f"{BUILTIN_PREFIX}meta":
            return DispatchResult(ok=True, result=server_meta(registry, version=version))
        return DispatchResult(
            ok=False,
            status=404,
            error={"kind": "UnknownMethodError", "message": f"no method {method!r}"},
        )

    if method in FORBIDDEN_METHODS:
        # Not 404: pretending it does not exist would be a lie a maintainer would
        # then have to debug. It exists and is refused.
        return DispatchResult(
            ok=False,
            status=403,
            error={
                "kind": "ForbiddenMethodError",
                "message": f"{method} is never exposed to a frontend",
            },
        )

    if not registry.methods.has(method):
        return DispatchResult(
            ok=False,
            status=404,
            error={"kind": "UnknownMethodError", "message": f"no method {method!r}"},
        )

    try:
        result = await registry.methods.call(method, **kwargs)
    except TypeError as exc:
        # A wrong argument name is the frontend's mistake, not the backend's fault.
        return DispatchResult(
            ok=False,
            status=400,
            error={"kind": "TypeError", "message": redact(f"{method}: {exc}")},
        )
    except UnknownMethodError as exc:
        return DispatchResult(
            ok=False, status=404, error=describe_error(exc), retry_after=None
        )
    except PoedexError as exc:
        error = describe_error(exc)
        retry_after = error.get("retry_after")
        status = 429 if retry_after is not None else 400
        return DispatchResult(ok=False, status=status, error=error, retry_after=retry_after)
    except Exception as exc:  # pragma: no cover - defensive
        return DispatchResult(ok=False, status=500, error=describe_error(exc))

    return DispatchResult(ok=True, result=result)


def describe_error(exc: BaseException) -> dict[str, Any]:
    """An exception as a wire error: class name, redacted message, retry hint.

    ``retry_after`` is read off the exception rather than pattern-matched from the
    message, because both `net.RateLimited` and `poeapi.RateLimitedError` carry it
    as an attribute and a surface needs the number to run a countdown against.
    """
    retry_after = getattr(exc, "retry_after", None)
    return {
        "kind": type(exc).__name__,
        "message": redact(str(exc)) or type(exc).__name__,
        "retry_after": float(retry_after) if isinstance(retry_after, (int, float)) else None,
    }
