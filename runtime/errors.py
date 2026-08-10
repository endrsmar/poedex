"""Runtime exception hierarchy.

Everything the runtime raises derives from :class:`PoedexError` so a surface can
catch one type and know the message is already redacted (see :mod:`runtime.log`).
"""

from __future__ import annotations

from collections.abc import Sequence


class PoedexError(Exception):
    """Base for every error raised by the runtime and its modules."""


class ModuleError(PoedexError):
    """A problem with a module's declaration, wiring or lifecycle."""


class InvalidModuleError(ModuleError):
    """A module object does not satisfy the contract in IMPLEMENTATION-PLAN §4."""


class DuplicateModuleError(ModuleError):
    """Two modules claim the same id, or two claim the same provided API."""


class DependencyCycleError(ModuleError):
    """`requires` edges form a cycle. A hard startup error (plan §1.5)."""

    def __init__(self, cycle: Sequence[str]) -> None:
        self.cycle = list(cycle)
        super().__init__("dependency cycle: " + " -> ".join([*self.cycle, self.cycle[0]]))


class KindViolationError(ModuleError):
    """A core module declares a dependency on a feature module (plan §1.5)."""

    def __init__(self, module_id: str, dependency_id: str) -> None:
        self.module_id = module_id
        self.dependency_id = dependency_id
        super().__init__(
            f"core module {module_id!r} may not require feature module {dependency_id!r}"
        )


class UndeclaredDependencyError(ModuleError):
    """A module asked the context for an API it did not declare in `requires`."""


class ProviderNotFoundError(ModuleError):
    """No started module provides the requested API type."""


class ModuleNotStartedError(ModuleError):
    """An operation needs a started module and it is not running."""


class MethodError(PoedexError):
    """A problem with the method registry."""


class UnknownMethodError(MethodError):
    """Nothing is registered under that qualified name."""


class SettingsError(PoedexError):
    """A settings schema or value is invalid."""


class UnknownSettingError(SettingsError):
    """A key that no registered schema declares."""


class StorageError(PoedexError):
    """A namespaced storage operation failed or was refused."""
