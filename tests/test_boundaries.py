"""The guardrail (IMPLEMENTATION-PLAN §2.6).

Two independent checks, because they catch different mistakes:

* **Static** — every Python file is parsed and its imports are checked against the
  layer rules. This catches the import.
* **Runtime** — the assembled registry is checked for cycles and core→feature edges.
  This catches the declaration.

The checker is written as functions over an arbitrary root, so the tests can point
it at a synthetic tree that violates each rule on purpose. That is what makes these
tests trustworthy: ``test_checker_catches_*`` proves the checker fails when it
should, and ``test_real_tree_is_clean`` proves the real tree passes. A checker only
ever run against clean code is indistinguishable from one that always returns [].
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from runtime.errors import DependencyCycleError, KindViolationError
from runtime.registry import Registry
from tests.conftest import REPO_ROOT

# Top-level directories, by layer. Kept explicit rather than inferred: a new
# top-level directory should have to state which layer it belongs to.
RUNTIME_DIR = "runtime"
MODULES_DIR = "modules"
SURFACE_DIRS = ("cli", "surfaces", "transports", "plugin")

# The runtime hosts modules and knows nothing about PoE, so it may not reach
# downward or sideways into anything that does.
RUNTIME_FORBIDDEN_ROOTS = {MODULES_DIR, "transports", "surfaces", "cli", "plugin", "ui_kit"}

# Decky's API is available only inside the Decky plugin host. A module importing it
# would work on hardware and fail everywhere else, which is how a module stops being
# portable between the two surfaces.
DECKY_ROOTS = {"decky", "decky_plugin", "decky_loader"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


@dataclass(frozen=True)
class Manifest:
    """A module's declaration, read from the AST rather than by importing it.

    Static, so it works on a synthetic tree and cannot be fooled by a module that
    mutates its own ``requires`` at import time.
    """

    id: str
    kind: str
    requires: tuple[str, ...]


# -- import extraction ---------------------------------------------------------


def python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if any(part in {"__pycache__", ".venv", "node_modules"} for part in path.parts):
            continue
        yield path


def _package_of(path: Path, root: Path) -> list[str]:
    """Dotted package parts of the file's own package, for relative imports."""
    return list(path.relative_to(root).parent.parts)


def imports_of(path: Path, root: Path) -> list[tuple[int, str]]:
    """Every dotted import target in ``path``, as ``(lineno, dotted)``.

    ``from a.b import c`` yields ``a.b.c`` — not ``a.b`` — so that
    ``from modules.x.backend import api`` is recognised as reaching ``api`` and not
    misread as reaching into the backend package itself.
    """
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import: resolve against the file's package
                package = _package_of(path, root)
                base_parts = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                base = ".".join([*base_parts, *( [node.module] if node.module else [])])
            else:
                base = node.module or ""
            for alias in node.names:
                found.append((node.lineno, f"{base}.{alias.name}" if base else alias.name))
    return found


def _root_of(dotted: str) -> str:
    return dotted.split(".", 1)[0]


def _is_within(dotted: str, prefix: str) -> bool:
    return dotted == prefix or dotted.startswith(prefix + ".")


# -- manifests -----------------------------------------------------------------


def read_manifest(module_dir: Path) -> Manifest | None:
    """Read id/kind/requires out of ``<module_dir>/backend/module.py``."""
    source = module_dir / "backend" / "module.py"
    if not source.is_file():
        return None
    tree = ast.parse(source.read_text("utf-8"), filename=str(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        fields: dict[str, object] = {}
        for stmt in node.body:
            target, value = None, None
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                if isinstance(stmt.targets[0], ast.Name):
                    target, value = stmt.targets[0].id, stmt.value
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target, value = stmt.target.id, stmt.value
            if target in {"id", "kind", "requires"} and value is not None:
                try:
                    fields[target] = ast.literal_eval(value)
                except ValueError:
                    continue
        if {"id", "kind"} <= set(fields):
            return Manifest(
                id=str(fields["id"]),
                kind=str(fields["kind"]),
                requires=tuple(fields.get("requires") or ()),
            )
    raise AssertionError(f"{source} has no class declaring id and kind")


def manifests(root: Path) -> dict[str, Manifest]:
    modules_dir = root / MODULES_DIR
    if not modules_dir.is_dir():
        return {}
    found: dict[str, Manifest] = {}
    for entry in sorted(modules_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = read_manifest(entry)
        if manifest is None:
            continue
        assert manifest.id == entry.name, (
            f"{entry}: directory name and module id disagree ({manifest.id!r})"
        )
        found[manifest.id] = manifest
    return found


# -- the rules -----------------------------------------------------------------


def check_runtime_is_self_contained(root: Path) -> list[Violation]:
    """`runtime` imports nothing from `modules` or `transports`."""
    violations: list[Violation] = []
    runtime_dir = root / RUNTIME_DIR
    if not runtime_dir.is_dir():
        return violations
    for path in python_files(runtime_dir):
        for line, dotted in imports_of(path, root):
            if _root_of(dotted) in RUNTIME_FORBIDDEN_ROOTS:
                violations.append(
                    Violation(path, line, f"runtime must not import {dotted!r}")
                )
    return violations


def check_module_imports(root: Path) -> list[Violation]:
    """Cross-module imports: declared, of the right kind, and api.py only."""
    violations: list[Violation] = []
    known = manifests(root)
    for module_id, manifest in known.items():
        module_dir = root / MODULES_DIR / module_id
        for path in python_files(module_dir):
            for line, dotted in imports_of(path, root):
                violations.extend(
                    _check_one_import(path, line, dotted, manifest, known)
                )
    return violations


def _check_one_import(
    path: Path, line: int, dotted: str, manifest: Manifest, known: dict[str, Manifest]
) -> list[Violation]:
    if _root_of(dotted) in DECKY_ROOTS:
        return [Violation(path, line, f"module {manifest.id} must not import a Decky API ({dotted})")]
    if _root_of(dotted) in SURFACE_DIRS:
        return [Violation(path, line, f"module {manifest.id} must not import the {dotted!r} surface")]
    if _root_of(dotted) != MODULES_DIR:
        return []

    parts = dotted.split(".")
    if len(parts) < 2:
        return [Violation(path, line, f"{manifest.id} imports the modules package itself")]
    other = parts[1]
    if other == manifest.id:
        return []

    problems: list[Violation] = []
    if other not in manifest.requires:
        problems.append(
            Violation(
                path,
                line,
                f"{manifest.id} imports {other!r} but does not declare it in requires="
                f"{list(manifest.requires)}",
            )
        )
    dep = known.get(other)
    if dep is None:
        problems.append(Violation(path, line, f"{manifest.id} imports unknown module {other!r}"))
    elif manifest.kind == "core" and dep.kind == "feature":
        problems.append(
            Violation(path, line, f"core module {manifest.id} imports feature module {other}")
        )
    if not _is_within(dotted, f"{MODULES_DIR}.{other}.backend.api"):
        problems.append(
            Violation(
                path,
                line,
                f"{manifest.id} may import only {MODULES_DIR}.{other}.backend.api, not {dotted}",
            )
        )
    return problems


def check_declared_kinds(root: Path) -> list[Violation]:
    """A `kind: "core"` module declaring a feature dependency, seen statically."""
    violations: list[Violation] = []
    known = manifests(root)
    for module_id, manifest in known.items():
        source = root / MODULES_DIR / module_id / "backend" / "module.py"
        for dep in manifest.requires:
            target = known.get(dep)
            if target is None:
                violations.append(Violation(source, 0, f"{module_id} requires unknown module {dep!r}"))
            elif manifest.kind == "core" and target.kind == "feature":
                violations.append(
                    Violation(source, 0, f"core module {module_id} requires feature module {dep}")
                )
    return violations


def check_surfaces(root: Path) -> list[Violation]:
    """Surfaces may host modules, but still only through their api.py."""
    violations: list[Violation] = []
    known = manifests(root)
    for surface in SURFACE_DIRS:
        surface_dir = root / surface
        if not surface_dir.is_dir():
            continue
        for path in python_files(surface_dir):
            for line, dotted in imports_of(path, root):
                if _root_of(dotted) != MODULES_DIR:
                    continue
                parts = dotted.split(".")
                if len(parts) < 2 or parts[1] not in known:
                    violations.append(Violation(path, line, f"{surface} imports unknown {dotted!r}"))
                    continue
                if not _is_within(dotted, f"{MODULES_DIR}.{parts[1]}.backend.api"):
                    violations.append(
                        Violation(
                            path,
                            line,
                            f"{surface} may import only a module's api.py, not {dotted}",
                        )
                    )
    return violations


def check_all(root: Path) -> list[Violation]:
    return [
        *check_runtime_is_self_contained(root),
        *check_module_imports(root),
        *check_declared_kinds(root),
        *check_surfaces(root),
    ]


# -- static checks against the real tree ---------------------------------------


def test_checker_actually_sees_the_source_tree():
    """Guard against a checker that passes because it found nothing to check."""
    assert len(list(python_files(REPO_ROOT / RUNTIME_DIR))) >= 5
    # Equality, not containment: this is the inventory. A module that silently
    # stops being discovered must be loud. The cost is one conflicting line per
    # phase, which is a feature — the module set should not change unnoticed.
    assert set(manifests(REPO_ROOT)) == {"credentials", "gamelog", "net", "poeapi"}
    creds = REPO_ROOT / MODULES_DIR / "credentials" / "backend" / "module.py"
    assert any(dotted.startswith("runtime.") for _, dotted in imports_of(creds, REPO_ROOT))
    # A real cross-module edge has to be visible to the checker, or the api.py rule
    # is only ever proved against a synthetic tree.
    poeapi = REPO_ROOT / MODULES_DIR / "poeapi" / "backend" / "module.py"
    assert any(
        dotted.startswith("modules.net.backend.api")
        for _, dotted in imports_of(poeapi, REPO_ROOT)
    )


def test_real_tree_is_clean():
    violations = check_all(REPO_ROOT)
    assert not violations, "boundary violations:\n" + "\n".join(str(v) for v in violations)


def test_runtime_imports_no_module():
    assert not check_runtime_is_self_contained(REPO_ROOT)


def test_no_module_imports_decky():
    for module_dir in (REPO_ROOT / MODULES_DIR).iterdir():
        if not module_dir.is_dir():
            continue
        for path in python_files(module_dir):
            for _, dotted in imports_of(path, REPO_ROOT):
                assert _root_of(dotted) not in DECKY_ROOTS, f"{path} imports {dotted}"


# -- the checker must fail on a violation --------------------------------------
#
# Each of these builds a source tree that breaks exactly one rule and asserts the
# checker reports it. Without them, "the boundary tests pass" would mean nothing.

CLEAN_ALPHA = '''
"""Core module alpha."""
from runtime.context import ModuleContext


class AlphaModule:
    id = "alpha"
    name = "Alpha"
    kind = "core"
    requires: list[str] = []
    provides = None


MODULE = AlphaModule()
'''

CLEAN_BETA = '''
"""Feature module beta, which legitimately depends on alpha."""
from modules.alpha.backend.api import AlphaApi


class BetaModule:
    id = "beta"
    name = "Beta"
    kind = "feature"
    requires = ["alpha"]
    provides = None


MODULE = BetaModule()
'''


def _write(root: Path, relative: str, source: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def _tree(root: Path) -> Path:
    """A minimal, clean two-module tree: core `alpha`, feature `beta`."""
    _write(root, "runtime/__init__.py", "")
    _write(root, "runtime/context.py", "class ModuleContext:\n    pass\n")
    _write(root, "modules/__init__.py", "")
    _write(root, "modules/alpha/backend/api.py", "class AlphaApi:\n    pass\n")
    _write(root, "modules/alpha/backend/module.py", CLEAN_ALPHA)
    _write(root, "modules/beta/backend/api.py", "class BetaApi:\n    pass\n")
    _write(root, "modules/beta/backend/module.py", CLEAN_BETA)
    return root


def test_fixture_tree_is_clean(tmp_path: Path):
    """The control case. If this tree failed, every test below would be vacuous."""
    assert check_all(_tree(tmp_path)) == []


def test_checker_catches_runtime_importing_a_module(tmp_path: Path):
    root = _tree(tmp_path)
    _write(root, "runtime/leaky.py", "from modules.alpha.backend.api import AlphaApi\n")
    assert any("runtime must not import" in v.message for v in check_all(root))


def test_checker_catches_core_importing_a_feature(tmp_path: Path):
    root = _tree(tmp_path)
    _write(
        root,
        "modules/alpha/backend/module.py",
        CLEAN_ALPHA.replace('requires: list[str] = []', 'requires = ["beta"]').replace(
            "from runtime.context import ModuleContext",
            "from modules.beta.backend.api import BetaApi",
        ),
    )
    messages = [v.message for v in check_all(root)]
    assert any("core module alpha imports feature module beta" in m for m in messages)
    assert any("core module alpha requires feature module beta" in m for m in messages)


def test_checker_catches_importing_past_api(tmp_path: Path):
    root = _tree(tmp_path)
    _write(root, "modules/alpha/backend/store.py", "SECRET = 1\n")
    _write(
        root,
        "modules/beta/backend/module.py",
        CLEAN_BETA.replace(
            "from modules.alpha.backend.api import AlphaApi",
            "from modules.alpha.backend.store import SECRET",
        ),
    )
    assert any("may import only" in v.message for v in check_all(root))


def test_checker_catches_undeclared_dependency(tmp_path: Path):
    root = _tree(tmp_path)
    _write(
        root,
        "modules/beta/backend/module.py",
        CLEAN_BETA.replace('requires = ["alpha"]', "requires: list[str] = []"),
    )
    assert any("does not declare it in requires" in v.message for v in check_all(root))


def test_checker_catches_decky_import(tmp_path: Path):
    root = _tree(tmp_path)
    _write(root, "modules/beta/backend/panel.py", "import decky\n")
    assert any("must not import a Decky API" in v.message for v in check_all(root))


def test_checker_catches_module_importing_a_surface(tmp_path: Path):
    root = _tree(tmp_path)
    _write(root, "modules/beta/backend/oops.py", "from cli.main import build_parser\n")
    assert any("must not import the" in v.message for v in check_all(root))


def test_checker_catches_surface_importing_past_api(tmp_path: Path):
    root = _tree(tmp_path)
    _write(root, "modules/alpha/backend/store.py", "SECRET = 1\n")
    _write(root, "cli/main.py", "from modules.alpha.backend.store import SECRET\n")
    assert any("only a module's api.py" in v.message for v in check_all(root))


def test_checker_resolves_relative_imports(tmp_path: Path):
    """A relative import must not be a way around the rule."""
    root = _tree(tmp_path)
    _write(root, "runtime/leaky.py", "from ..modules.alpha.backend import api\n")
    # `from ..x` inside runtime/ resolves above the package; the checker treats the
    # resolved target, not the literal text.
    _write(root, "modules/beta/backend/sneaky.py", "from ...alpha.backend.store import SECRET\n")
    messages = [v.message for v in check_all(root)]
    assert any("alpha" in m for m in messages)


# -- runtime checks on an assembled registry -----------------------------------


def test_registry_rejects_a_cycle(registry, fake_module):
    registry.register(fake_module("a", requires=["b"]))
    registry.register(fake_module("b", requires=["a"]))
    with pytest.raises(DependencyCycleError) as excinfo:
        registry.resolve()
    assert set(excinfo.value.cycle) == {"a", "b"}
    assert any("cycle" in problem for problem in registry.check_boundaries())


def test_registry_rejects_core_depending_on_feature(registry, fake_module):
    registry.register(fake_module("shiny", kind="feature"))
    registry.register(fake_module("plumbing", kind="core", requires=["shiny"]))
    with pytest.raises(KindViolationError):
        registry.resolve()
    problems = registry.check_boundaries()
    assert any("core module plumbing requires feature module shiny" in p for p in problems)


def test_assembled_real_registry_has_no_boundary_problems():
    real = Registry()
    real.load(REPO_ROOT / MODULES_DIR)
    assert real.check_boundaries() == []
    assert real.resolve() == ["credentials", "gamelog", "net", "poeapi"]


def test_every_shipped_module_is_core_for_now():
    """Phases 1 and 2 ship only core modules; the first feature module lands in Phase 3.

    Stated as a test because "core may not depend on a feature" is unfalsifiable
    while no feature module exists, and it should be obvious when that changes.
    """
    kinds = {mid: m.kind for mid, m in manifests(REPO_ROOT).items()}
    assert kinds == {
        "credentials": "core",
        "gamelog": "core",
        "net": "core",
        "poeapi": "core",
    }
