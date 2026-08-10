"""Namespaced on-disk storage.

Each module gets a directory of its own and can only address keys inside it. Writes
are atomic (temp file in the same directory, then ``os.replace``) so a crash or a
suspend mid-write cannot leave a half-written cache behind — the Deck suspends
whenever the user closes the lid.

Directory resolution, first match wins:

1. an explicit root passed to :class:`StorageRoot`
2. ``POEDEX_CACHE_DIR``
3. ``DECKY_PLUGIN_RUNTIME_DIR`` (set by Decky Loader, Phase 7)
4. ``$XDG_CACHE_HOME/poedex`` or ``~/.cache/poedex``
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from runtime.errors import StorageError

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._-]+$")

CONFIG_DIR_ENV = "POEDEX_CONFIG_DIR"
CACHE_DIR_ENV = "POEDEX_CACHE_DIR"

# Owner-only, per SPEC §8. Applied to every directory and file we create, because a
# cache can hold account data even when it does not hold the credential itself.
DIR_MODE = 0o700
FILE_MODE = 0o600


def config_dir() -> Path:
    """Where user-owned configuration lives (``~/.config/poedex`` by default)."""
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    decky = os.environ.get("DECKY_PLUGIN_SETTINGS_DIR")
    if decky:
        return Path(decky).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "poedex"


def cache_dir() -> Path:
    """Where regenerable per-module data lives."""
    override = os.environ.get(CACHE_DIR_ENV)
    if override:
        return Path(override).expanduser()
    decky = os.environ.get("DECKY_PLUGIN_RUNTIME_DIR")
    if decky:
        return Path(decky).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(base).expanduser() / "poedex"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) owner-only, and tighten it if it already exists."""
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        if (path.stat().st_mode & 0o777) != DIR_MODE:
            path.chmod(DIR_MODE)
    except OSError:  # pragma: no cover - unusual filesystems
        pass
    return path


def write_private_file(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with mode 0600 and an owner-only parent."""
    ensure_private_dir(path.parent)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, FILE_MODE)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class Storage:
    """A module's private corner of the cache directory."""

    def __init__(self, root: Path, namespace: str) -> None:
        self.namespace = namespace
        self._dir = Path(root) / namespace

    @property
    def directory(self) -> Path:
        return self._dir

    def path(self, key: str) -> Path:
        """Absolute path for ``key``. Traversal outside the namespace is refused."""
        if not _SAFE_KEY.match(key) or key in {".", ".."}:
            raise StorageError(
                f"invalid storage key {key!r}: use letters, digits, dot, dash, underscore"
            )
        return self._dir / key

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def read_bytes(self, key: str) -> bytes | None:
        path = self.path(key)
        if not path.exists():
            return None
        return path.read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        write_private_file(self.path(key), data)

    def read_json(self, key: str, default: Any = None) -> Any:
        raw = self.read_bytes(key)
        if raw is None:
            return default
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StorageError(f"{self.namespace}/{key} is not valid JSON: {exc}") from None

    def write_json(self, key: str, value: Any) -> None:
        payload = json.dumps(value, indent=2, sort_keys=True, default=str).encode("utf-8")
        self.write_bytes(key, payload)

    def delete(self, key: str) -> bool:
        path = self.path(key)
        if not path.exists():
            return False
        path.unlink()
        return True

    def keys(self) -> list[str]:
        if not self._dir.exists():
            return []
        # Dot-prefixed names are the in-flight temp files of an atomic write.
        return sorted(
            p.name
            for p in self._dir.iterdir()
            if p.is_file() and not p.name.startswith(".") and _SAFE_KEY.match(p.name)
        )

    def clear(self) -> None:
        for key in self.keys():
            self.delete(key)

    def __repr__(self) -> str:
        return f"Storage(namespace={self.namespace!r})"


class StorageRoot:
    """Hands out :class:`Storage` namespaces. One per runtime."""

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root).expanduser() if root is not None else cache_dir()
        self._namespaces: dict[str, Storage] = {}

    def namespace(self, module_id: str) -> Storage:
        if module_id not in self._namespaces:
            if not _SAFE_KEY.match(module_id):
                raise StorageError(f"invalid storage namespace {module_id!r}")
            self._namespaces[module_id] = Storage(self.root, module_id)
        return self._namespaces[module_id]

    def __repr__(self) -> str:
        return f"StorageRoot(root={str(self.root)!r})"
