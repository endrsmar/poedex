"""The import pin in `plugin/main.py`, tested against the failure it exists for.

There was already a test asserting the string ``typing_extensions`` appeared in
``plugin/main.py``. It passed, and the plugin still died on the first Deck install
with::

    ImportError: cannot import name 'Sentinel' from 'typing_extensions'
    (/tmp/_MEI6LlfHP/setuptools/_vendor/typing_extensions.py)

A test that a mitigation is *mentioned* is not a test that it *works*. The original
fix prepended ``py_modules`` to ``sys.path`` and purged the stale module, which is
sound reasoning about ``sys.path`` and irrelevant to what actually happened:
**``sys.meta_path`` finders are consulted before ``sys.path`` is looked at at all**,
so setuptools' vendor importer wins regardless of ordering.

These tests run the real prologue of the real file in a subprocess, against a
deliberately hostile import environment, and assert which file the module came from.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "plugin" / "main.py"

PROLOGUE_END = "# ---"
"""The prologue is everything above the rule that closes it. Taking it from the file
rather than restating it means this test cannot pass against a `main.py` that has
quietly stopped doing the thing."""


def prologue() -> str:
    """The part of `main.py` that runs before any third-party import."""
    source = MAIN.read_text("utf-8")
    marker = "\n" + PROLOGUE_END
    cut = source.index(marker, source.index("must run before any third-party import"))
    body = source[:cut]
    # Drop the module docstring and the __future__ import: this is executed as a
    # fragment, and `from __future__` is only legal at the top of a module.
    return "\n".join(
        line for line in body.splitlines() if not line.startswith("from __future__")
    )


def run(script: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=60,
    )


@pytest.fixture
def plugin_tree(tmp_path: Path) -> Path:
    """A plugin directory whose vendored `typing_extensions` is identifiable."""
    py_modules = tmp_path / "py_modules"
    py_modules.mkdir()
    (py_modules / "typing_extensions.py").write_text(
        "ORIGIN = 'py_modules'\nSentinel = object()\n", encoding="utf-8"
    )
    return tmp_path


HOSTILE_META_PATH = textwrap.dedent(
    """
    import importlib.abc, importlib.util, sys, types

    class Hijack(importlib.abc.MetaPathFinder, importlib.abc.Loader):
        '''What setuptools' vendor importer does, reduced to its essence.

        A meta_path finder is asked before any sys.path entry, so this shadows
        typing_extensions no matter where py_modules sits in the search order.
        '''
        def find_spec(self, fullname, path=None, target=None):
            if fullname != 'typing_extensions':
                return None
            return importlib.util.spec_from_loader(fullname, self)

        def create_module(self, spec):
            module = types.ModuleType(spec.name)
            module.ORIGIN = 'setuptools/_vendor'   # no Sentinel, exactly like the real one
            return module

        def exec_module(self, module):
            pass

    sys.meta_path.insert(0, Hijack())
    """
)


def test_a_meta_path_hijack_loses_to_the_pin(plugin_tree: Path):
    """The real failure, reproduced: a finder that beats every sys.path arrangement."""
    script = (
        HOSTILE_META_PATH
        + f"\nimport os\nos.environ['DECKY_PLUGIN_DIR'] = {str(plugin_tree)!r}\n"
        + prologue()
        + "\nimport typing_extensions\nprint(typing_extensions.ORIGIN)\n"
    )
    result = run(script, plugin_tree)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "py_modules", (
        "the hijacking meta_path finder won; the pin does not defeat the thing it "
        f"exists to defeat. stderr:\n{result.stderr}"
    )


def test_an_already_imported_shadow_loses_to_the_pin(plugin_tree: Path):
    """The other half: the stale module is already in sys.modules before we run."""
    script = (
        "import sys, types\n"
        "stale = types.ModuleType('typing_extensions')\n"
        "stale.ORIGIN = 'setuptools/_vendor'\n"
        "sys.modules['typing_extensions'] = stale\n"
        + f"import os\nos.environ['DECKY_PLUGIN_DIR'] = {str(plugin_tree)!r}\n"
        + prologue()
        + "\nimport typing_extensions\nprint(typing_extensions.ORIGIN)\n"
    )
    result = run(script, plugin_tree)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "py_modules"


def test_the_pin_is_a_no_op_when_nothing_is_vendored(tmp_path: Path):
    """A plugin tree with no vendored copy must not crash on the way past.

    `--no-vendor` builds exist for iteration, and the prologue runs in them too.
    """
    (tmp_path / "py_modules").mkdir()
    script = (
        f"import os\nos.environ['DECKY_PLUGIN_DIR'] = {str(tmp_path)!r}\n"
        + prologue()
        + "\nprint('survived')\n"
    )
    result = run(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "survived"


def test_a_broken_vendored_module_does_not_leave_a_half_built_import(tmp_path: Path):
    """If our own copy raises, it must not be left in sys.modules as a usable shell."""
    py_modules = tmp_path / "py_modules"
    py_modules.mkdir()
    (py_modules / "typing_extensions.py").write_text(
        "raise RuntimeError('boom')\n", encoding="utf-8"
    )
    script = (
        f"import os, sys\nos.environ['DECKY_PLUGIN_DIR'] = {str(tmp_path)!r}\n"
        "try:\n"
        + textwrap.indent(prologue(), "    ")
        + "\nexcept RuntimeError:\n"
        "    print('raised', 'typing_extensions' in sys.modules)\n"
    )
    result = run(script, tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "raised False", (
        "a failed load left the module in sys.modules, so every later import would "
        "get an empty shell instead of the real error"
    )
