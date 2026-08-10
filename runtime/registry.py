"""Module registry: discovery, ordering, validation, lifecycle.

Behaviour fixed by IMPLEMENTATION-PLAN §1.5:

* topological sort on ``requires``; **cycles are a hard startup error**
* start in dependency order, stop in reverse
* a missing or disabled dependency **disables the dependent with a stated reason**,
  retrievable from :meth:`Registry.status` — never a silent partial start
* a ``kind: "core"`` module declaring a feature dependency is a startup error

The registry knows nothing about Path of Exile. It hosts modules.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from runtime.context import ModuleContext
from runtime.errors import (
    DependencyCycleError,
    DuplicateModuleError,
    InvalidModuleError,
    KindViolationError,
    ModuleError,
    ProviderNotFoundError,
    UndeclaredDependencyError,
)
from runtime.events import EventBus
from runtime.log import get_logger
from runtime.methods import MethodRegistry
from runtime.module import Module, validate_module
from runtime.settings import SettingsStore
from runtime.storage import StorageRoot

T = TypeVar("T")

_log = get_logger("runtime.registry")

MODULE_ATTR = "MODULE"
BACKEND_PACKAGE = "backend"
MODULE_FILE = "module.py"


class ModuleState(StrEnum):
    REGISTERED = "registered"
    STARTED = "started"
    STOPPED = "stopped"
    DISABLED = "disabled"
    FAILED = "failed"


@dataclass
class ModuleRecord:
    module: Module
    state: ModuleState = ModuleState.REGISTERED
    reason: str | None = None
    context: ModuleContext | None = field(default=None, repr=False)

    @property
    def id(self) -> str:
        return self.module.id

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.module.id,
            "name": self.module.name,
            "kind": self.module.kind,
            "requires": list(self.module.requires),
            "state": self.state.value,
            "reason": self.reason,
        }


def discover(modules_root: Path | str, package: str = "modules") -> list[Module]:
    """Import every ``modules/<id>/backend/module.py`` and collect its ``MODULE``.

    Discovery is by convention and deliberately dumb: one directory per module, one
    exported instance per module. Nothing scans for subclasses, so nothing is
    registered by accident.
    """
    root = Path(modules_root)
    found: list[Module] = []
    if not root.is_dir():
        return found
    for entry in sorted(root.iterdir()):
        if not (entry / BACKEND_PACKAGE / MODULE_FILE).is_file():
            continue
        dotted = f"{package}.{entry.name}.{BACKEND_PACKAGE}.module"
        imported = importlib.import_module(dotted)
        instance = getattr(imported, MODULE_ATTR, None)
        if instance is None:
            raise ModuleError(f"{dotted} does not export {MODULE_ATTR}")
        found.append(instance)
    return found


class Registry:
    """Holds modules and the runtime services they are given."""

    def __init__(
        self,
        *,
        events: EventBus | None = None,
        storage: StorageRoot | None = None,
        settings: SettingsStore | None = None,
        methods: MethodRegistry | None = None,
        disabled: Iterable[str] = (),
    ) -> None:
        self.events = events or EventBus()
        self.storage = storage or StorageRoot()
        self.settings = settings or SettingsStore()
        self.methods = methods or MethodRegistry()
        self._disabled_by_user = set(disabled)
        self._records: dict[str, ModuleRecord] = {}
        self._providers: dict[type, str] = {}
        self._order: list[str] = []
        self._started: list[str] = []

    # -- registration ----------------------------------------------------------

    def register(self, module: Module) -> None:
        validate_module(module)
        if module.id in self._records:
            raise DuplicateModuleError(f"module id {module.id!r} registered twice")
        provides = getattr(module, "provides", None)
        if provides is not None:
            try:
                satisfied = isinstance(module, provides)
            except TypeError:
                # A Protocol without @runtime_checkable cannot be verified here; the
                # type checker is the backstop for those.
                satisfied = True
            if not satisfied:
                raise InvalidModuleError(
                    f"{module.id!r} declares provides={provides.__name__} but does not "
                    "implement it"
                )
            owner = self._providers.get(provides)
            if owner is not None:
                raise DuplicateModuleError(
                    f"{module.id!r} and {owner!r} both provide {provides.__name__}"
                )
            self._providers[provides] = module.id
        self._records[module.id] = ModuleRecord(module=module)

    def register_all(self, modules: Iterable[Module]) -> None:
        for module in modules:
            self.register(module)

    def load(self, modules_root: Path | str, package: str = "modules") -> None:
        self.register_all(discover(modules_root, package))

    # -- introspection ---------------------------------------------------------

    def __contains__(self, module_id: object) -> bool:
        return module_id in self._records

    def get(self, module_id: str) -> Module:
        return self._records[module_id].module

    def record(self, module_id: str) -> ModuleRecord:
        return self._records[module_id]

    def ids(self) -> list[str]:
        return sorted(self._records)

    @property
    def order(self) -> list[str]:
        """Resolved start order. Empty until :meth:`resolve` runs."""
        return list(self._order)

    def status(self) -> dict[str, dict[str, Any]]:
        """Every module's state and, when disabled, the reason why."""
        return {mid: rec.to_json() for mid, rec in sorted(self._records.items())}

    def reason(self, module_id: str) -> str | None:
        return self._records[module_id].reason

    def is_started(self, module_id: str) -> bool:
        rec = self._records.get(module_id)
        return rec is not None and rec.state is ModuleState.STARTED

    def api(self, protocol: type[T]) -> T:
        """Look up a provided API without going through a module context."""
        module_id = self._providers.get(protocol)
        if module_id is None or not self.is_started(module_id):
            raise ProviderNotFoundError(f"no started module provides {protocol.__name__}")
        return self._records[module_id].module  # type: ignore[return-value]

    # -- resolution ------------------------------------------------------------

    def resolve(self) -> list[str]:
        """Validate kinds, detect cycles, disable unsatisfiable modules, and order.

        Returns the ids that will actually start, in dependency order.
        """
        self._check_kinds()
        order = self._toposort()
        self._apply_disables(order)
        self._order = [mid for mid in order if self._records[mid].state is ModuleState.REGISTERED]
        return list(self._order)

    def _check_kinds(self) -> None:
        for rec in self._records.values():
            module = rec.module
            if module.kind != "core":
                continue
            for dep_id in module.requires:
                dep = self._records.get(dep_id)
                if dep is not None and dep.module.kind == "feature":
                    raise KindViolationError(module.id, dep_id)

    def _toposort(self) -> list[str]:
        """Kahn's algorithm over `requires`, ties broken by id for a stable order."""
        indegree = {mid: 0 for mid in self._records}
        dependents: dict[str, list[str]] = {mid: [] for mid in self._records}
        for mid, rec in self._records.items():
            for dep in rec.module.requires:
                if dep in self._records:
                    indegree[mid] += 1
                    dependents[dep].append(mid)

        ready = sorted(mid for mid, deg in indegree.items() if deg == 0)
        order: list[str] = []
        while ready:
            current = ready.pop(0)
            order.append(current)
            for dependent in sorted(dependents[current]):
                indegree[dependent] -= 1
                if indegree[dependent] == 0:
                    ready.append(dependent)
            ready.sort()

        if len(order) != len(self._records):
            remaining = {mid for mid in self._records if mid not in order}
            raise DependencyCycleError(self._find_cycle(remaining))
        return order

    def _find_cycle(self, candidates: set[str]) -> list[str]:
        """Return one concrete cycle, so the error names the modules involved."""
        path: list[str] = []
        on_path: set[str] = set()
        visited: set[str] = set()

        def walk(node: str) -> list[str] | None:
            if node in on_path:
                return path[path.index(node):]
            if node in visited:
                return None
            visited.add(node)
            on_path.add(node)
            path.append(node)
            for dep in self._records[node].module.requires:
                if dep in candidates:
                    found = walk(dep)
                    if found:
                        return found
            path.pop()
            on_path.discard(node)
            return None

        for start in sorted(candidates):
            cycle = walk(start)
            if cycle:
                # `requires` points at the dependency, so walking it yields the cycle
                # backwards relative to start order. Present it in start order.
                return list(reversed(cycle))
        return sorted(candidates)  # pragma: no cover - unreachable if a cycle exists

    def _apply_disables(self, order: list[str]) -> None:
        """Mark user-disabled modules, then cascade to everything that needed them."""
        for mid in self._disabled_by_user:
            rec = self._records.get(mid)
            if rec is None:
                raise ModuleError(f"cannot disable unknown module {mid!r}")
            if rec.module.kind == "core":
                raise ModuleError(f"core module {mid!r} cannot be disabled")
            self._disable(rec, "disabled by user")

        for mid in order:
            rec = self._records[mid]
            if rec.state is not ModuleState.REGISTERED:
                continue
            for dep_id in rec.module.requires:
                dep = self._records.get(dep_id)
                if dep is None:
                    self._disable(rec, f"requires {dep_id!r}, which is not installed")
                    break
                if dep.state is not ModuleState.REGISTERED:
                    self._disable(
                        rec,
                        f"requires {dep_id!r}, which is {dep.state.value}"
                        + (f" ({dep.reason})" if dep.reason else ""),
                    )
                    break

    def _disable(self, rec: ModuleRecord, reason: str) -> None:
        rec.state = ModuleState.DISABLED
        rec.reason = reason
        _log.warning("module %s disabled: %s", rec.id, reason)

    # -- lifecycle -------------------------------------------------------------

    async def start_all(self) -> None:
        """Start every startable module in dependency order."""
        if not self._order:
            self.resolve()
        for module_id in self._order:
            await self._start_one(self._records[module_id])

    async def _start_one(self, rec: ModuleRecord) -> None:
        module = rec.module
        ctx = ModuleContext(
            module_id=module.id,
            events=self.events,
            storage=self.storage.namespace(module.id),
            settings=self.settings.register(module.id, module.settings_schema()),
            logger=get_logger(f"module.{module.id}"),
            _resolve=self._resolve_api,
        )
        rec.context = ctx
        try:
            await module.start(ctx)
            self.methods.register_all(module.id, module.methods())
        except Exception as exc:
            rec.state = ModuleState.FAILED
            rec.reason = f"{type(exc).__name__}: {exc}"
            self.methods.unregister_module(module.id)
            _log.exception("module %s failed to start", module.id)
            raise
        rec.state = ModuleState.STARTED
        rec.reason = None
        self._started.append(module.id)

    async def stop_all(self) -> None:
        """Stop started modules in reverse start order. One failure does not
        prevent the rest from stopping."""
        errors: list[BaseException] = []
        for module_id in reversed(self._started):
            rec = self._records[module_id]
            try:
                await rec.module.stop()
            except Exception as exc:
                errors.append(exc)
                _log.exception("module %s failed to stop", module_id)
            finally:
                self.methods.unregister_module(module_id)
                rec.state = ModuleState.STOPPED
                rec.context = None
        self._started.clear()
        if errors:
            raise ExceptionGroup("module shutdown failed", errors)

    # -- dependency resolution -------------------------------------------------

    def _resolve_api(self, module_id: str, protocol: type) -> Any:
        provider_id = self._providers.get(protocol)
        if provider_id is None:
            raise ProviderNotFoundError(
                f"{module_id}: no module provides {getattr(protocol, '__name__', protocol)!r}"
            )
        requires = self._records[module_id].module.requires
        if provider_id not in requires and provider_id != module_id:
            raise UndeclaredDependencyError(
                f"{module_id} requested {protocol.__name__} from {provider_id!r}, "
                f"which it does not declare in requires={list(requires)}"
            )
        provider = self._records[provider_id]
        if provider.state is not ModuleState.STARTED:
            raise ProviderNotFoundError(
                f"{module_id}: {provider_id!r} is {provider.state.value}"
                + (f" ({provider.reason})" if provider.reason else "")
            )
        return provider.module

    # -- checks used by the boundary tests -------------------------------------

    def check_boundaries(self) -> list[str]:
        """Declaration-level violations on the assembled registry (plan §2.6).

        Static analysis catches the import; this catches the declaration. Returns a
        list of human-readable problems rather than raising, so a test can report
        all of them at once.
        """
        problems: list[str] = []
        for mid, rec in sorted(self._records.items()):
            module = rec.module
            for dep in module.requires:
                if dep not in self._records:
                    problems.append(f"{mid} requires {dep!r}, which is not registered")
                elif module.kind == "core" and self._records[dep].module.kind == "feature":
                    problems.append(f"core module {mid} requires feature module {dep}")
        try:
            self._toposort()
        except DependencyCycleError as exc:
            problems.append(str(exc))
        return problems


def build_registry(
    modules_root: Path | str,
    *,
    package: str = "modules",
    disabled: Iterable[str] = (),
    **services: Any,
) -> Registry:
    """Convenience: a registry with every discovered module registered."""
    registry = Registry(disabled=disabled, **services)
    registry.load(modules_root, package)
    return registry


def declared_graph(modules: Mapping[str, Module]) -> dict[str, list[str]]:
    """`requires` edges as a plain dict. Used by tests and diagnostics."""
    return {mid: list(m.requires) for mid, m in modules.items()}
