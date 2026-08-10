"""What a module is handed at start (IMPLEMENTATION-PLAN §4).

The runtime services — events, storage, settings, logger — are *not* modules and are
not declared in ``requires``; they are what the context is made of. The one thing a
module must declare is a dependency on another module, and it reaches it through
:meth:`ModuleContext.require`, typed by the dependency's Protocol.

``require`` refuses any API that the module did not declare in ``requires``, so an
undeclared edge fails at start rather than becoming an invisible coupling that the
static boundary tests cannot see.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from logging import Logger
from typing import Any, TypeVar

from runtime.events import EventBus
from runtime.settings import SettingsView
from runtime.storage import Storage

T = TypeVar("T")

Resolver = Callable[[str, type], Any]


@dataclass(frozen=True)
class ModuleContext:
    module_id: str
    events: EventBus
    storage: Storage
    settings: SettingsView
    logger: Logger
    _resolve: Resolver = field(repr=False)

    def require(self, api: type[T]) -> T:
        """Return the implementation of ``api`` provided by a declared dependency.

        Raises :class:`~runtime.errors.UndeclaredDependencyError` if this module did
        not declare the providing module in ``requires``, and
        :class:`~runtime.errors.ProviderNotFoundError` if nothing provides it.
        """
        return self._resolve(self.module_id, api)
