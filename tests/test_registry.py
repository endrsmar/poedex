"""Registry: ordering, cycles, kinds, disabling, lifecycle, typed resolution."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest

from runtime.errors import (
    DependencyCycleError,
    DuplicateModuleError,
    InvalidModuleError,
    KindViolationError,
    ModuleError,
    ProviderNotFoundError,
    UndeclaredDependencyError,
)
from runtime.registry import ModuleState, Registry, discover
from tests.conftest import REPO_ROOT


@runtime_checkable
class GreeterApi(Protocol):
    async def greet(self) -> str: ...


class Greeter:
    """A module that provides a typed API to a dependent."""

    id = "greeter"
    name = "Greeter"
    kind = "core"
    requires: list[str] = []  # noqa: RUF012
    provides = GreeterApi

    async def start(self, ctx) -> None:
        self.ctx = ctx

    async def stop(self) -> None: ...

    def methods(self):
        return {"greet": self.greet}

    def settings_schema(self):
        return {}

    async def greet(self) -> str:
        return "hello"


# -- ordering ------------------------------------------------------------------


def test_toposort_starts_dependencies_first(registry, fake_module):
    registry.register(fake_module("appraisal", requires=["prices"]))
    registry.register(fake_module("prices", requires=["poeapi"]))
    registry.register(fake_module("poeapi", kind="core"))
    assert registry.resolve() == ["poeapi", "prices", "appraisal"]


def test_toposort_is_deterministic_for_independent_modules(registry, fake_module):
    for module_id in ("zulu", "alpha", "mike"):
        registry.register(fake_module(module_id, kind="core"))
    assert registry.resolve() == ["alpha", "mike", "zulu"]


async def test_lifecycle_starts_in_order_and_stops_in_reverse(registry, fake_module):
    log: list[str] = []
    registry.register(fake_module("base", kind="core", log=log))
    registry.register(fake_module("middle", requires=["base"], log=log))
    registry.register(fake_module("top", requires=["middle"], log=log))

    await registry.start_all()
    assert log == ["start:base", "start:middle", "start:top"]
    assert all(registry.is_started(m) for m in ("base", "middle", "top"))

    await registry.stop_all()
    assert log[3:] == ["stop:top", "stop:middle", "stop:base"]
    assert registry.record("top").state is ModuleState.STOPPED


# -- cycles --------------------------------------------------------------------


def test_direct_cycle_is_a_hard_error(registry, fake_module):
    registry.register(fake_module("a", requires=["b"]))
    registry.register(fake_module("b", requires=["a"]))
    with pytest.raises(DependencyCycleError) as excinfo:
        registry.resolve()
    assert set(excinfo.value.cycle) == {"a", "b"}
    assert "->" in str(excinfo.value)


def test_indirect_cycle_names_every_module_in_it(registry, fake_module):
    registry.register(fake_module("a", requires=["b"]))
    registry.register(fake_module("b", requires=["c"]))
    registry.register(fake_module("c", requires=["a"]))
    registry.register(fake_module("bystander", kind="core"))
    with pytest.raises(DependencyCycleError) as excinfo:
        registry.resolve()
    assert set(excinfo.value.cycle) == {"a", "b", "c"}
    assert "bystander" not in str(excinfo.value)


def test_self_dependency_is_rejected_at_registration(registry, fake_module):
    with pytest.raises(InvalidModuleError):
        registry.register(fake_module("narcissus", requires=["narcissus"]))


# -- kinds ---------------------------------------------------------------------


def test_core_requiring_a_feature_is_a_startup_error(registry, fake_module):
    registry.register(fake_module("appraisal", kind="feature"))
    registry.register(fake_module("net", kind="core", requires=["appraisal"]))
    with pytest.raises(KindViolationError) as excinfo:
        registry.resolve()
    assert excinfo.value.module_id == "net"
    assert excinfo.value.dependency_id == "appraisal"


def test_feature_requiring_core_is_fine(registry, fake_module):
    registry.register(fake_module("net", kind="core"))
    registry.register(fake_module("prices", kind="feature", requires=["net"]))
    assert registry.resolve() == ["net", "prices"]


def test_unknown_kind_is_rejected(registry, fake_module):
    with pytest.raises(InvalidModuleError):
        registry.register(fake_module("weird", kind="middleware"))


# -- missing and disabled dependencies -----------------------------------------


def test_missing_dependency_disables_the_dependent_with_a_reason(registry, fake_module):
    registry.register(fake_module("appraisal", requires=["prices"]))
    assert registry.resolve() == []
    record = registry.record("appraisal")
    assert record.state is ModuleState.DISABLED
    assert "prices" in record.reason
    assert "not installed" in record.reason


def test_disabling_cascades_and_states_the_original_reason(registry_factory, fake_module):
    registry = registry_factory(disabled=["prices"])
    registry.register(fake_module("poeapi", kind="core"))
    registry.register(fake_module("prices", requires=["poeapi"]))
    registry.register(fake_module("appraisal", requires=["prices"]))

    assert registry.resolve() == ["poeapi"]
    assert registry.reason("prices") == "disabled by user"
    reason = registry.reason("appraisal")
    assert "prices" in reason and "disabled" in reason


async def test_disabled_module_is_not_started(registry_factory, fake_module):
    log: list[str] = []
    registry = registry_factory(disabled=["optional"])
    registry.register(fake_module("optional", log=log))
    await registry.start_all()
    assert log == []
    await registry.stop_all()
    assert log == []


def test_core_modules_cannot_be_disabled(registry_factory, fake_module):
    registry = registry_factory(disabled=["net"])
    registry.register(fake_module("net", kind="core"))
    with pytest.raises(ModuleError, match="cannot be disabled"):
        registry.resolve()


def test_disabling_an_unknown_module_is_an_error(registry_factory, fake_module):
    registry = registry_factory(disabled=["ghost"])
    registry.register(fake_module("real", kind="core"))
    with pytest.raises(ModuleError, match="unknown module"):
        registry.resolve()


# -- registration validation ---------------------------------------------------


def test_duplicate_ids_are_rejected(registry, fake_module):
    registry.register(fake_module("twin", kind="core"))
    with pytest.raises(DuplicateModuleError):
        registry.register(fake_module("twin", kind="core"))


def test_two_modules_cannot_provide_the_same_api(registry, fake_module):
    registry.register(Greeter())
    impostor = fake_module("impostor", kind="core", provides=GreeterApi)
    impostor.greet = lambda: None
    with pytest.raises(DuplicateModuleError, match="both provide"):
        registry.register(impostor)


def test_declaring_an_api_it_does_not_implement_is_rejected(registry, fake_module):
    with pytest.raises(InvalidModuleError, match="does not implement"):
        registry.register(fake_module("liar", kind="core", provides=GreeterApi))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"module_id": "Bad-Id"},
        {"module_id": "9lives"},
        {"module_id": ""},
    ],
)
def test_ids_must_be_lowercase_identifiers(registry, fake_module, kwargs):
    with pytest.raises(InvalidModuleError):
        registry.register(fake_module(**kwargs))


def test_a_module_missing_a_lifecycle_method_is_rejected(registry):
    class HalfBaked:
        id = "halfbaked"
        name = "Half Baked"
        kind = "core"
        requires: list[str] = []  # noqa: RUF012
        provides = None

        async def start(self, ctx) -> None: ...

        def methods(self):
            return {}

        def settings_schema(self):
            return {}

    with pytest.raises(InvalidModuleError, match="stop"):
        registry.register(HalfBaked())


# -- typed dependency resolution -----------------------------------------------


async def test_a_module_resolves_a_typed_dependency_on_another(registry, fake_module):
    """Phase 1 exit criterion: a second module resolves a dependency on the first."""
    registry.register(Greeter())
    dependent = fake_module("polite", requires=["greeter"], wants=[GreeterApi])
    registry.register(dependent)

    await registry.start_all()

    resolved = dependent.resolved[GreeterApi]
    assert isinstance(resolved, GreeterApi)
    assert await resolved.greet() == "hello"
    assert resolved is registry.get("greeter")


async def test_requiring_an_undeclared_api_fails_at_start(registry, fake_module):
    registry.register(Greeter())
    registry.register(fake_module("rude", requires=[], wants=[GreeterApi]))
    with pytest.raises(UndeclaredDependencyError, match="does not declare"):
        await registry.start_all()


async def test_requiring_an_unprovided_api_fails_at_start(registry, fake_module):
    registry.register(fake_module("lonely", kind="core", wants=[GreeterApi]))
    with pytest.raises(ProviderNotFoundError):
        await registry.start_all()


def test_api_lookup_before_start_is_refused(registry):
    registry.register(Greeter())
    with pytest.raises(ProviderNotFoundError):
        registry.api(GreeterApi)


# -- methods and settings wiring ----------------------------------------------


async def test_methods_are_namespaced_by_module_id(registry):
    registry.register(Greeter())
    await registry.start_all()
    assert registry.methods.names() == ["greeter.greet"]
    assert await registry.methods.call("greeter.greet") == "hello"

    await registry.stop_all()
    assert registry.methods.names() == []


async def test_a_module_that_fails_to_start_is_marked_failed(registry, fake_module):
    registry.register(fake_module("broken", kind="core", fail_on_start=True))
    with pytest.raises(RuntimeError):
        await registry.start_all()
    record = registry.record("broken")
    assert record.state is ModuleState.FAILED
    assert "refuses to start" in record.reason
    assert registry.methods.names() == []


async def test_one_module_failing_to_stop_does_not_block_the_others(registry, fake_module):
    log: list[str] = []
    registry.register(fake_module("base", kind="core", log=log))
    registry.register(fake_module("bad", requires=["base"], log=log, fail_on_stop=True))
    await registry.start_all()
    with pytest.raises(ExceptionGroup):
        await registry.stop_all()
    assert log[-1] == "stop:base"


# -- discovery -----------------------------------------------------------------


def test_discovery_finds_the_credentials_module():
    found = discover(REPO_ROOT / "modules")
    assert [m.id for m in found] == ["credentials"]
    assert found[0].kind == "core"


def test_discovery_of_an_empty_tree_is_not_an_error(tmp_path):
    assert discover(tmp_path) == []
    assert discover(tmp_path / "does-not-exist") == []


def test_a_module_without_the_module_attribute_is_an_error(tmp_path, monkeypatch):
    backend = tmp_path / "pkg" / "ghost" / "backend"
    backend.mkdir(parents=True)
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "ghost" / "__init__.py").write_text("")
    (backend / "__init__.py").write_text("")
    (backend / "module.py").write_text("# no MODULE here\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(ModuleError, match="does not export MODULE"):
        discover(tmp_path / "pkg", package="pkg")


def test_build_from_the_real_tree_starts_credentials():
    registry = Registry()
    registry.load(REPO_ROOT / "modules")
    assert registry.resolve() == ["credentials"]
    assert registry.record("credentials").state is ModuleState.REGISTERED
