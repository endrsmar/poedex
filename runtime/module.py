"""The module contract (IMPLEMENTATION-PLAN §4).

A module is a vertical slice with a declaration the registry can reason about
*before* anything starts: its id, its kind, what it requires, and the Protocol it
provides to dependents.

``Module`` is a structural Protocol rather than a base class on purpose — a module
should be able to be a plain object, and the registry validates the declaration
explicitly in :func:`validate_module` instead of relying on inheritance.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from runtime.errors import InvalidModuleError

ModuleKind = Literal["core", "feature"]
KINDS: frozenset[str] = frozenset({"core", "feature"})

_ID = re.compile(r"^[a-z][a-z0-9_]*$")


@runtime_checkable
class Module(Protocol):
    id: str
    name: str
    kind: ModuleKind
    requires: list[str]
    provides: type | None

    async def start(self, ctx: Any) -> None: ...

    async def stop(self) -> None: ...

    def methods(self) -> dict[str, Callable[..., Any]]: ...

    def settings_schema(self) -> dict[str, Any]: ...


def validate_module(module: object) -> None:
    """Raise :class:`InvalidModuleError` unless ``module`` satisfies the contract."""
    where = getattr(module, "id", None) or type(module).__name__

    module_id = getattr(module, "id", None)
    if not isinstance(module_id, str) or not _ID.match(module_id):
        raise InvalidModuleError(
            f"{where}: 'id' must be a lowercase identifier matching {_ID.pattern}"
        )

    if not isinstance(getattr(module, "name", None), str):
        raise InvalidModuleError(f"{module_id}: 'name' must be a str")

    kind = getattr(module, "kind", None)
    if kind not in KINDS:
        raise InvalidModuleError(
            f"{module_id}: 'kind' must be one of {sorted(KINDS)}, got {kind!r}"
        )

    requires = getattr(module, "requires", None)
    if not isinstance(requires, (list, tuple)) or not all(isinstance(r, str) for r in requires):
        raise InvalidModuleError(f"{module_id}: 'requires' must be a list of module ids")
    if module_id in requires:
        raise InvalidModuleError(f"{module_id}: must not require itself")
    if len(set(requires)) != len(requires):
        raise InvalidModuleError(f"{module_id}: duplicate entries in 'requires'")

    provides = getattr(module, "provides", "__missing__")
    if provides != "__missing__" and provides is not None and not isinstance(provides, type):
        raise InvalidModuleError(f"{module_id}: 'provides' must be a type or None")

    for attr in ("start", "stop", "methods", "settings_schema"):
        if not callable(getattr(module, attr, None)):
            raise InvalidModuleError(f"{module_id}: missing required method {attr}()")
