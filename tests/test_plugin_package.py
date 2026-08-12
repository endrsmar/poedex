"""What the Decky plugin package must be true of, without building one.

`scripts/build_plugin.py` needs network access once (to `pip download` wheels for a
CPython this machine is not running), so the build itself is not a test. What *is*
testable is every rule the build enforces, and those are the rules that would
otherwise be discovered on hardware:

* the manifest keeps `debug` and never carries `root`;
* an extension built for the wrong CPython fails the build rather than the Deck;
* the entry point exposes the five methods the frontend calls, all `async def`, none
  starting with an underscore, and no path to the credential.

`plugin/main.py` cannot be imported here — it imports `decky` — so it is read as an
AST, which is the same technique `tests/test_boundaries.py` uses and for the same
reason.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from scripts.build_plugin import (
    FRONTEND,
    SOURCE_EXCLUDES,
    SOURCE_PACKAGES,
    TARGET_PYTHON,
    VENDOR,
    _assert_abi,
    copy_manifest,
)
from tests.conftest import REPO_ROOT

PLUGIN = REPO_ROOT / "plugin"


def manifest() -> dict:
    return json.loads((PLUGIN / "plugin.json").read_text())


def main_module() -> ast.Module:
    return ast.parse((PLUGIN / "main.py").read_text())


def plugin_class() -> ast.ClassDef:
    for node in main_module().body:
        if isinstance(node, ast.ClassDef) and node.name == "Plugin":
            return node
    raise AssertionError("plugin/main.py has no class called Plugin")


# -- the manifest ---------------------------------------------------------------


def test_the_debug_flag_is_kept():
    """Decky's hot reload refuses to watch a plugin without it (SPEC §7), and
    hardware iteration is the only way this surface can be developed at all."""
    assert "debug" in manifest()["flags"]


def test_root_is_never_enabled():
    """Plugins run as `deck`, and every path this tool reads — Client.txt, the
    settings directory, the cache — is readable without root (SPEC §8)."""
    assert "root" not in manifest()["flags"]


def test_the_build_refuses_a_root_flag(tmp_path: Path, monkeypatch):
    """The rule is enforced at build time too, not only asserted about the file.

    A `root` flag added in a hurry and reverted after the zip was cut is exactly the
    shape of mistake that ships.
    """
    fake = tmp_path / "plugin"
    fake.mkdir()
    payload = manifest()
    payload["flags"] = ["debug", "root"]
    (fake / "plugin.json").write_text(json.dumps(payload))
    (fake / "main.py").write_text("")

    import scripts.build_plugin as build

    monkeypatch.setattr(build, "REPO", tmp_path)
    with pytest.raises(SystemExit, match="root flag"):
        copy_manifest(tmp_path / "out", "0.1.0")


def test_the_build_refuses_a_manifest_that_dropped_debug(tmp_path: Path, monkeypatch):
    fake = tmp_path / "plugin"
    fake.mkdir()
    payload = manifest()
    payload["flags"] = []
    (fake / "plugin.json").write_text(json.dumps(payload))
    (fake / "main.py").write_text("")

    import scripts.build_plugin as build

    monkeypatch.setattr(build, "REPO", tmp_path)
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(SystemExit, match="debug flag"):
        copy_manifest(out, "0.1.0")


# -- the vendored extension -----------------------------------------------------


def test_the_build_refuses_an_extension_built_for_another_cpython(tmp_path: Path):
    """`pydantic-core` publishes **no abi3 wheel**, so the ABI is pinned to one
    CPython minor and the plugin process runs the frozen loader's 3.11 — not the
    Deck's system python, which is 3.13 from SteamOS 3.7. A `cp313` build is not even
    *found* by a 3.11 interpreter, so the Deck-side symptom is
    `ModuleNotFoundError: pydantic_core`, which reads as "you forgot to vendor it".
    """
    modules = tmp_path / "py_modules" / "pydantic_core"
    modules.mkdir(parents=True)
    (modules / "_pydantic_core.cpython-313-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    with pytest.raises(SystemExit, match="wrong CPython"):
        _assert_abi(tmp_path / "py_modules")


def test_the_build_refuses_a_py_modules_with_no_extension_at_all(tmp_path: Path):
    empty = tmp_path / "py_modules"
    empty.mkdir()
    with pytest.raises(SystemExit, match="Rust extension"):
        _assert_abi(empty)


def test_an_abi3_extension_would_be_accepted(tmp_path: Path):
    """Stated as a test because it is the outcome everyone wants and nobody has.

    If `pydantic-core` ever publishes abi3 wheels, this build stops caring which
    CPython Decky freezes against, and `TARGET_PYTHON` becomes a floor rather than a
    pin. Until then it is a pin, and the docstring on `TARGET_PYTHON` says so.
    """
    modules = tmp_path / "py_modules" / "somepkg"
    modules.mkdir(parents=True)
    (modules / "_ext.abi3.so").write_bytes(b"\x7fELF")
    _assert_abi(tmp_path / "py_modules")


def test_the_vendor_list_is_the_projects_runtime_dependencies():
    """`pyproject.toml`'s base dependency set is what ships.

    Not a duplicate list to keep in sync: adding a runtime dependency there is what
    makes it land in `py_modules/`, and `web` extras deliberately stay out — the
    Decky transport uses Decky's RPC and the pairing listener is
    `asyncio.start_server`, so nothing on this surface needs FastAPI.
    """
    text = (REPO_ROOT / "pyproject.toml").read_text()
    declared = {
        line.split(">=")[0].strip().strip('"') for line in text.splitlines() if ">=" in line
    }
    for requirement in VENDOR:
        assert requirement.split(">=")[0] in declared
    assert not any("fastapi" in requirement or "uvicorn" in requirement for requirement in VENDOR)


def test_tests_and_module_ui_are_not_shipped():
    assert "tests" in SOURCE_EXCLUDES
    assert "ui" in SOURCE_EXCLUDES


def test_the_deck_preview_never_reaches_the_zip():
    """`surfaces/deck-preview/` is a dev tool and must stay one.

    It exists to render the QAM panel in a browser, so it carries a stand-in for
    `@decky/ui`, a fixture transport and a page full of chrome — everything the real
    plugin must not have. There are exactly two doors into the package and this
    closes both: the Python side copies four named packages and none of them is
    under `surfaces/`, and the frontend side copies **one file**, the compact bundle
    Rollup wrote. A third check covers the way it could get in anyway, which is the
    plugin's own entry importing it.
    """
    assert not any(package.startswith("surfaces") for package in SOURCE_PACKAGES)
    assert FRONTEND == REPO_ROOT / "surfaces" / "decky" / "dist" / "index.js"

    decky_source = REPO_ROOT / "surfaces" / "decky" / "src"
    for path in decky_source.rglob("*.ts*"):
        assert "deck-preview" not in path.read_text(), f"{path} imports the preview"


def test_the_target_python_is_the_loaders_not_the_decks():
    # SteamOS 3.7+ ships 3.13; Decky Loader is frozen against 3.11.7 and a plugin
    # backend is a fork of the loader. Getting this backwards is the whole failure.
    assert TARGET_PYTHON == "3.11"


# -- the entry point ------------------------------------------------------------


def test_every_frontend_callable_is_async_and_public():
    """Decky's rule: frontend-callable methods are `async def` and must not start
    with `_` (SPEC §6.2)."""
    public = [
        node
        for node in plugin_class().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert {node.name for node in public} == {"call", "meta", "latest", "health"}
    for node in public:
        assert isinstance(node, ast.AsyncFunctionDef), f"{node.name} must be async def"


def test_the_lifecycle_hooks_exist_and_are_async():
    hooks = {
        node.name
        for node in plugin_class().body
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_")
    }
    assert {"_main", "_unload"} <= hooks


def test_no_frontend_callable_could_return_a_credential():
    """There is no `pair_submit` and no reader.

    The credential arrives over the pairing socket from the *other* machine, so no
    method the panel can call carries one in and none can read one back. Asserted on
    the names because a method that does not exist cannot be called by accident.
    """
    names = {
        node.name
        for node in plugin_class().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for forbidden in ("session_id", "reveal", "credential", "secret", "pair_submit"):
        assert not any(forbidden in name for name in names)


def test_py_modules_is_prepended_before_any_third_party_import():
    """The loader *appends* `py_modules`, so PyInstaller's bundled
    `setuptools/_vendor/typing_extensions` shadows the one pydantic needs — observed
    as `ImportError: cannot import name 'Sentinel'`. The fix has to run first, and
    "first" is a property of the file rather than of a function.
    """
    def prepends(node: ast.AST) -> bool:
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "insert"
                and isinstance(inner.func.value, ast.Attribute)
                and inner.func.value.attr == "path"
            ):
                return True
        return False

    body = main_module().body
    inserts = [index for index, node in enumerate(body) if prepends(node)]
    assert inserts, "plugin/main.py must prepend py_modules to sys.path"
    third_party = [
        index
        for index, node in enumerate(body)
        if isinstance(node, ast.Import)
        and any(alias.name in {"decky", "httpx", "pydantic"} for alias in node.names)
    ]
    assert third_party, "expected `import decky`"
    assert max(inserts) < min(third_party)


def test_the_typing_extensions_shadow_is_cleared():
    assert "typing_extensions" in (PLUGIN / "main.py").read_text()
