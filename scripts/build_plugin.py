"""Assemble the Decky plugin zip.

    python scripts/build_plugin.py            # dist/poedex/ and dist/poedex.zip
    python scripts/build_plugin.py --no-zip   # the directory only, for rsync
    python scripts/build_plugin.py --check    # is a built tree still current?

## The vendoring rule, and the measurement behind it

There is no pip at install time, so every dependency ships inside the zip under
`py_modules/`. Most of them are pure Python and this is uninteresting. **pydantic is
not**: `pydantic-core` is a compiled Rust extension, and — measured against PyPI —
it publishes **no `abi3` wheel at any version**. Every CPython wheel it has ever
shipped is tagged for one minor version (`cp310`…`cp315`).

So the wheel has to match the interpreter *the plugin process runs under*, and that
interpreter is **not** the Deck's `python3`:

* Decky Loader ships as a PyInstaller binary pinned to **CPython 3.11.7**
  (`.github/workflows/build.yml`), and a plugin backend is a `multiprocessing.Process`
  **fork of the loader itself** — so it runs the loader's embedded interpreter.
* SteamOS's own `/usr/bin/python3` is 3.11.7 on SteamOS 3.6 and **3.13** from
  SteamOS 3.7 onward. The loader did not follow. It also splices the system
  `sys.path` into the frozen interpreter, so `/usr/lib/python3.13/site-packages` is
  visible from a 3.11 process — pure-Python packages import from it, compiled ones
  cannot.

Verified inside `ghcr.io/steamdeckhomebrew/holo-base` against the real
`PluginLoader` binary: `EXTENSION_SUFFIXES` in a live plugin process is
`['.cpython-311-x86_64-linux-gnu.so', '.abi3.so', '.so']`; a `cp313` build of
`_pydantic_core` is not found at all, and renaming it to `.so` gets as far as
`ImportError: undefined symbol: PyDict_GetItemRef`. The `cp311` wheel works.

:data:`TARGET_PYTHON` is therefore **3.11**, and it is a *fact about Decky Loader*,
not about the Deck. **If Decky reships against a different CPython, this build breaks
and the symptom is a plugin that starts and does nothing** — which is why
`plugin/main.py` checks the import itself and emits `backend.broken` with the version
in it rather than dying in a log file.

## What is not vendored, and why

`fastapi` and `uvicorn`. They are the *web* transport's, the Decky transport uses
Decky's own RPC, and the pairing listener is deliberately `asyncio.start_server` so
that it needs neither (SPEC §4.1). Keeping them out of `py_modules/` is what keeps
this a two-dependency vendor rather than a twelve-dependency one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "dist"
NAME = "poedex"

TARGET_PYTHON = "3.11"
"""The frozen Decky Loader's CPython. See this module's docstring — it is not the
Deck's system Python, and picking that one instead is the failure this constant
exists to prevent."""

TARGET_PLATFORM = "manylinux_2_17_x86_64"
"""The Deck is x86_64 and SteamOS is glibc; `manylinux_2_17` is what pydantic-core
publishes and what the loader's own runtime satisfies."""

#: Vendored into `py_modules/`. Their transitive dependencies come along; the list
#: is what `pyproject.toml` declares as the base dependency set, on purpose, so
#: adding a runtime dependency there is what makes it ship.
VENDOR = ("httpx>=0.27", "pydantic>=2.7")

#: Python packages copied in whole. `cli` is included so `python -m cli.main` works
#: over SSH on the Deck, which is the only debugging surface gaming mode has.
SOURCE_PACKAGES = ("runtime", "modules", "transports", "cli")

#: Never shipped. `ui/` is TypeScript already inside `dist/index.js`; `tests/` is
#: not a product; `data/` is kept, because `moddb.json` is the module's whole point.
SOURCE_EXCLUDES = ("__pycache__", "tests", "ui", "node_modules", ".pytest_cache")

FRONTEND = REPO / "surfaces" / "decky" / "dist" / "index.js"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def clean(target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)


def copy_sources(target: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        del directory
        return {name for name in names if name in SOURCE_EXCLUDES}

    for package in SOURCE_PACKAGES:
        source = REPO / package
        if not source.is_dir():
            raise SystemExit(f"missing source package: {source}")
        shutil.copytree(source, target / package, ignore=ignore)


def vendor(target: Path) -> None:
    """`pip download` + unpack, rather than `pip install --target`.

    `--target` on a cross-version download refuses to run the wheel's install hooks
    and silently drops the ones it cannot; unpacking the wheels ourselves means what
    lands in `py_modules/` is exactly what the wheel contains, and the `cp311` tag is
    checked afterwards rather than hoped for.
    """
    modules = target / "py_modules"
    modules.mkdir(parents=True, exist_ok=True)
    wheels = target / ".wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--quiet",
        "--only-binary=:all:",
        "--platform",
        TARGET_PLATFORM,
        "--python-version",
        TARGET_PYTHON,
        "--implementation",
        "cp",
        "--abi",
        f"cp{TARGET_PYTHON.replace('.', '')}",
        "--dest",
        str(wheels),
        *VENDOR,
    ]
    log(f"vendoring for CPython {TARGET_PYTHON} / {TARGET_PLATFORM} ...")
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(
            "pip download failed. This build needs network access once, to fetch "
            f"wheels for CPython {TARGET_PYTHON}:\n{result.stderr.strip()}"
        )

    for wheel in sorted(wheels.glob("*.whl")):
        with zipfile.ZipFile(wheel) as archive:
            archive.extractall(modules)
    shutil.rmtree(wheels)

    # Nothing in `py_modules/` should carry a `.dist-info`; it is metadata for a
    # package manager that will never run here, and it is 400 KiB of it.
    for info in list(modules.glob("*.dist-info")) + list(modules.glob("*.data")):
        shutil.rmtree(info, ignore_errors=True)

    _assert_abi(modules)


def _assert_abi(modules: Path) -> None:
    """Fail the build rather than the Deck.

    A wheel built for the wrong CPython is not a crash on the Deck — it is a
    `ModuleNotFoundError`, because a `.cpython-313-*.so` is not in a 3.11
    interpreter's `EXTENSION_SUFFIXES` at all and the loader never even tries it.
    That reads as "you forgot to vendor pydantic", which is the wrong bug to go
    looking for, so it is caught here where the tag is visible.
    """
    tag = f"cpython-{TARGET_PYTHON.replace('.', '')}"
    extensions = sorted(modules.rglob("*.so"))
    if not extensions:
        raise SystemExit(
            "no compiled extension in py_modules/ — pydantic-core is a Rust "
            "extension and must be there. Did pip serve a pure-Python fallback?"
        )
    wrong = [path for path in extensions if tag not in path.name and ".abi3." not in path.name]
    if wrong:
        names = ", ".join(path.name for path in wrong)
        raise SystemExit(
            f"py_modules/ carries an extension built for the wrong CPython: {names}. "
            f"The Decky Loader plugin process runs CPython {TARGET_PYTHON}."
        )


def copy_manifest(target: Path, version: str) -> None:
    manifest = json.loads((REPO / "plugin" / "plugin.json").read_text())
    if "root" in manifest.get("flags", []):
        # SPEC §8. Plugins run as `deck` and every path this tool reads is readable
        # without it, so a `root` flag here would be privilege nobody asked for.
        raise SystemExit("plugin.json must not carry the root flag")
    if "debug" not in manifest.get("flags", []):
        # Decky's hot reload refuses to watch a plugin without it, and hardware
        # iteration is the only way this surface can be developed at all.
        raise SystemExit("plugin.json must keep the debug flag (SPEC §7)")
    (target / "plugin.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy2(REPO / "plugin" / "main.py", target / "main.py")
    (target / "package.json").write_text(
        json.dumps(
            {
                "name": manifest["name"],
                "version": version,
                "description": manifest["publish"]["description"],
                "type": "module",
                "license": "Apache-2.0",
            },
            indent=2,
        )
        + "\n"
    )
    for extra in ("LICENSE", "README.md"):
        source = REPO / extra
        if source.is_file():
            shutil.copy2(source, target / extra)


def copy_frontend(target: Path) -> None:
    if not FRONTEND.is_file():
        raise SystemExit(
            f"no frontend bundle at {FRONTEND}.\n"
            "  cd surfaces/decky && pnpm run build"
        )
    (target / "dist").mkdir(parents=True, exist_ok=True)
    shutil.copy2(FRONTEND, target / "dist" / "index.js")
    # The source map is the difference between a legible CEF traceback and a column
    # number in a minified line, on a surface nobody can attach a debugger to.
    source_map = FRONTEND.with_suffix(".js.map")
    if source_map.is_file():
        shutil.copy2(source_map, target / "dist" / "index.js.map")


def make_zip(target: Path, archive: Path) -> None:
    """One top-level directory named after the plugin — Decky expects that shape."""
    if archive.exists():
        archive.unlink()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                zf.write(path, Path(NAME) / path.relative_to(target))


def measure(target: Path, archive: Path | None) -> str:
    def size(paths: Iterable[Path]) -> int:
        return sum(path.stat().st_size for path in paths if path.is_file())

    # Each row counts a disjoint part of the tree: `py_modules/` is vendored, and
    # counting its 4 MB of `.py` under "python source" too would make the numbers
    # add up to more than the build.
    ours = [path for package in SOURCE_PACKAGES for path in (target / package).rglob("*")]
    rows = [
        ("py_modules", size((target / "py_modules").rglob("*"))),
        ("moddb data", size(path for path in ours if path.suffix == ".json")),
        ("python source", size(path for path in ours if path.suffix == ".py")),
        ("frontend", size((target / "dist").rglob("*"))),
    ]
    lines = [f"{name:<16}{value / 1024:>9.0f} KiB" for name, value in rows]
    lines.append(f"{'unpacked':<16}{size(target.rglob('*')) / 1024:>9.0f} KiB")
    if archive is not None and archive.exists():
        lines.append(f"{'zip':<16}{archive.stat().st_size / 1024:>9.0f} KiB  {archive}")
    return "\n".join(lines)


def fingerprint(target: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(target.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(target)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=str(OUT), help="build directory (default: dist/)")
    parser.add_argument("--version", default="0.1.0")
    parser.add_argument("--no-zip", action="store_true", help="leave the tree, skip the archive")
    parser.add_argument(
        "--no-vendor",
        action="store_true",
        help="skip py_modules/ — for a rebuild when only the source changed",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="print the fingerprint of an existing build instead of building",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    target = out / NAME
    archive = out / f"{NAME}.zip"

    if args.check:
        if not target.is_dir():
            log(f"no build at {target}")
            return 1
        print(fingerprint(target))
        return 0

    vendored = None
    if args.no_vendor and (target / "py_modules").is_dir():
        vendored = out / ".py_modules.keep"
        shutil.rmtree(vendored, ignore_errors=True)
        shutil.move(str(target / "py_modules"), str(vendored))

    clean(target)
    copy_sources(target)
    copy_manifest(target, args.version)
    copy_frontend(target)
    if vendored is not None:
        shutil.move(str(vendored), str(target / "py_modules"))
        log("reused the existing py_modules/")
    else:
        vendor(target)

    if not args.no_zip:
        make_zip(target, archive)

    log(measure(target, None if args.no_zip else archive))
    log(f"built {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
