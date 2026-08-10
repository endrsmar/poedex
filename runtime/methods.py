"""Method registry.

Modules expose callables through ``methods()``; the registry namespaces them by
module id, so ``credentials`` exposing ``set`` becomes ``credentials.set``. A
transport (HTTP in Phase 5, Decky RPC in Phase 7) is then a thin adapter over this
registry and holds no knowledge of any module.

Two rules from CLAUDE.md are enforced here rather than trusted: a frontend-callable
method is ``async def``, and its name does not start with ``_``.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from typing import Any

from runtime.errors import MethodError, UnknownMethodError


class MethodRegistry:
    def __init__(self) -> None:
        self._methods: dict[str, Callable[..., Any]] = {}
        self._owners: dict[str, str] = {}

    def register(self, module_id: str, name: str, fn: Callable[..., Any]) -> str:
        qualified = f"{module_id}.{name}"
        if name.startswith("_"):
            raise MethodError(f"{qualified}: method names must not start with '_'")
        if "." in name:
            raise MethodError(f"{qualified}: method names must not contain '.'")
        if not callable(fn):
            raise MethodError(f"{qualified}: not callable")
        if not inspect.iscoroutinefunction(_unwrap(fn)):
            raise MethodError(f"{qualified}: must be 'async def'")
        if qualified in self._methods:
            raise MethodError(f"{qualified}: already registered")
        self._methods[qualified] = fn
        self._owners[qualified] = module_id
        return qualified

    def register_all(self, module_id: str, methods: Mapping[str, Callable[..., Any]]) -> list[str]:
        if not isinstance(methods, Mapping):
            raise MethodError(f"{module_id}: methods() must return a dict")
        return [self.register(module_id, name, fn) for name, fn in methods.items()]

    def unregister_module(self, module_id: str) -> None:
        for qualified in [q for q, owner in self._owners.items() if owner == module_id]:
            del self._methods[qualified]
            del self._owners[qualified]

    def get(self, qualified: str) -> Callable[..., Any]:
        try:
            return self._methods[qualified]
        except KeyError:
            raise UnknownMethodError(f"no method {qualified!r}") from None

    def has(self, qualified: str) -> bool:
        return qualified in self._methods

    def names(self) -> list[str]:
        return sorted(self._methods)

    def for_module(self, module_id: str) -> list[str]:
        return sorted(q for q, owner in self._owners.items() if owner == module_id)

    async def call(self, qualified: str, /, *args: Any, **kwargs: Any) -> Any:
        return await self.get(qualified)(*args, **kwargs)

    def __len__(self) -> int:
        return len(self._methods)


def _unwrap(fn: Callable[..., Any]) -> Callable[..., Any]:
    """See through ``functools.partial`` and bound methods to the real function."""
    seen = 0
    while seen < 10:
        inner = getattr(fn, "func", None)
        if inner is None or inner is fn:
            return fn
        fn = inner
        seen += 1
    return fn
