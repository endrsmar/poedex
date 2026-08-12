"""Protocol conformance must not *call* anything on the module.

The bug this file exists for, in one line: the first Deck install died with
``ModuleNotStartedError: net has not been started`` raised from **registration**,
while 1369 tests were green.

`Registry.register` verifies `provides` by asking whether the module implements the
Protocol. A ``runtime_checkable`` Protocol's ``isinstance`` asks ``hasattr`` for each
member on **Python 3.11 and earlier**, and ``hasattr`` only swallows
``AttributeError``. `net` exposes `user_agent` as a property that raises
`ModuleNotStartedError` until `start()` — entirely reasonable — so the conformance
check detonated it before anything could start it.

It was invisible here because **Python 3.12 rewrote the check to use
`inspect.getattr_static`**, which does not invoke descriptors, and the venv is 3.12.
The Decky plugin host is the frozen loader's 3.11. That version gap is the real
finding: this whole class of bug is silent on the machine the tests run on.

So these tests assert the *property* — nothing is invoked — rather than asserting an
outcome that 3.12 would give us for free.
"""

from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

import pytest

from runtime.errors import InvalidModuleError, ModuleNotStartedError
from runtime.registry import Registry


class Detonated(Exception):
    """Raised by a property that should never have been read."""


@runtime_checkable
class ThingApi(Protocol):
    @property
    def thing(self) -> str: ...

    def method(self) -> None: ...


class RaisesUntilStarted:
    """A module shaped exactly like `net`: state behind a property, guarded."""

    id = "raiser"
    name = "Raiser"
    kind = "core"
    requires: ClassVar[list[str]] = []
    provides: type | None = ThingApi

    def __init__(self) -> None:
        self.started = False

    @property
    def thing(self) -> str:
        if not self.started:
            raise ModuleNotStartedError("raiser has not been started")
        return "ok"

    def method(self) -> None: ...

    async def start(self, ctx: object) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def methods(self) -> dict[str, object]:
        return {}

    def settings_schema(self) -> dict[str, object]:
        return {}


class Detonates(RaisesUntilStarted):
    """Same shape, but the property is fatal rather than merely unhappy.

    `ModuleNotStartedError` is the real case; this one proves the check never reads
    the property at all, rather than proving it happens to tolerate one exception type.
    """

    id = "detonator"

    @property
    def thing(self) -> str:
        raise Detonated("the conformance check read a property")


class MissingMember:
    id = "missing"
    name = "Missing"
    kind = "core"
    requires: ClassVar[list[str]] = []
    provides: type | None = ThingApi

    def method(self) -> None: ...

    async def start(self, ctx: object) -> None: ...

    async def stop(self) -> None: ...

    def methods(self) -> dict[str, object]:
        return {}

    def settings_schema(self) -> dict[str, object]:
        return {}


def test_registering_a_module_whose_property_raises_before_start(registry: Registry):
    """The exact Deck failure. Registration happens before anything is started."""
    registry.register(RaisesUntilStarted())
    assert "raiser" in registry.status()


def test_conformance_never_reads_a_property_at_all(registry: Registry):
    """Stronger, and version-independent: not "tolerates", but "does not invoke".

    On 3.12 this passes because `getattr_static` is used; on 3.11 it passes only
    because we stopped using `isinstance`. Either way a regression here is loud.
    """
    registry.register(Detonates())  # would raise Detonated if the property were read


def test_a_genuinely_missing_member_is_still_refused(registry: Registry):
    """The check must not have been softened into never failing."""
    with pytest.raises(InvalidModuleError, match="does not implement"):
        registry.register(MissingMember())


def test_the_property_still_raises_when_actually_used():
    """The guard itself is not what changed — only who is allowed to trip it."""
    module = RaisesUntilStarted()
    with pytest.raises(ModuleNotStartedError):
        _ = module.thing


# -- the version gap itself -----------------------------------------------------
#
# Everything above passes on 3.12 whether or not the fix is present, because 3.12's
# own `isinstance` already uses `getattr_static`. That is precisely how this shipped:
# a green suite on 3.12 says nothing about the 3.11 the plugin actually runs on.
#
# So this last test runs the real conformance helper under an interpreter that still
# has the old behaviour, and is skipped when none is installed. It is the only test
# in the file that can fail against the pre-fix code on this machine.

import shutil  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

OLD_PROTOCOL_BEHAVIOUR = ("python3.11", "python3.10", "python3.9")
"""Interpreters whose `runtime_checkable` isinstance still calls `hasattr`.

3.11 is the one that matters — it is what the frozen Decky Loader runs. 3.10 and 3.9
behave identically for this and are accepted as stand-ins, because the point is to
exercise the old code path, not a specific release.
"""


def _legacy_python() -> str | None:
    for name in OLD_PROTOCOL_BEHAVIOUR:
        found = shutil.which(name)
        if found:
            return found
    return None


def test_the_helper_holds_on_an_interpreter_that_still_calls_hasattr():
    """Run `_implements` under <=3.11 and prove the property is never read.

    Skipped rather than failed when no such interpreter exists: a machine without one
    genuinely cannot answer the question, and pretending otherwise is how the
    original bug survived 1369 green tests.
    """
    interpreter = _legacy_python()
    if interpreter is None:
        pytest.skip(
            "no <=3.11 interpreter available; this check cannot be answered here. "
            "The plugin host is 3.11 — see docs/deck-checklist.md item 2."
        )

    script = textwrap.dedent(
        f"""
        import sys
        sys.path.insert(0, {str(REPO)!r})
        import inspect, typing
        from typing import Protocol, runtime_checkable

        class Detonated(Exception): ...

        @runtime_checkable
        class ThingApi(Protocol):
            @property
            def thing(self) -> str: ...

        class Detonates:
            @property
            def thing(self) -> str:
                raise Detonated("the conformance check read a property")

        # the shipped helper, imported as source so this cannot drift from it
        source = open({str(REPO / "runtime" / "registry.py")!r}).read()
        start = source.index("def _implements(")
        end = source.index("class Registry:", start)
        namespace = {{}}
        exec(compile(source[start:end], "registry.py", "exec"), namespace)

        try:
            print("ok" if namespace["_implements"](Detonates(), ThingApi) else "refused")
        except Detonated:
            print("READ THE PROPERTY")
        """
    )
    result = subprocess.run(
        [interpreter, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok", (
        f"under {interpreter}: {result.stdout.strip()!r}. This is the Deck failure — "
        "conformance invoked a property that raises before start()."
    )
