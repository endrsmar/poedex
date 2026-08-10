"""Settings: a schema registry plus a small JSON persistence layer.

A module returns a plain dict from ``settings_schema()`` (plan §4), keyed by setting
name:

    {"session_max_age_days": {"type": "int", "default": 30, "min": 1, "max": 365,
                              "label": "Session age warning"}}

Supported keys: ``type`` (required, one of ``str int float bool list dict``),
``default`` (required), ``label``, ``description``, ``choices``, ``min``, ``max``.

Validation stays deliberately small — no jsonschema dependency, because everything
here has to be vendorable into ``py_modules/`` for the Decky backend, which has no
pip at install time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

from runtime.errors import SettingsError, UnknownSettingError
from runtime.storage import config_dir, write_private_file

_TYPES: dict[str, type | tuple[type, ...]] = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}

_ALLOWED_KEYS = {"type", "default", "label", "description", "choices", "min", "max"}

SETTINGS_FILENAME = "settings.json"

_UNSET: Any = object()


def validate_schema(module_id: str, schema: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Check a module's schema dict and return a normalized copy."""
    if not isinstance(schema, Mapping):
        raise SettingsError(f"{module_id}: settings_schema() must return a dict")
    normalized: dict[str, dict[str, Any]] = {}
    for key, spec in schema.items():
        where = f"{module_id}.{key}"
        if not isinstance(key, str) or not key:
            raise SettingsError(f"{module_id}: setting names must be non-empty strings")
        if not isinstance(spec, Mapping):
            raise SettingsError(f"{where}: setting spec must be a dict")
        unknown = set(spec) - _ALLOWED_KEYS
        if unknown:
            raise SettingsError(f"{where}: unknown spec keys {sorted(unknown)}")
        if "type" not in spec:
            raise SettingsError(f"{where}: missing 'type'")
        if spec["type"] not in _TYPES:
            raise SettingsError(f"{where}: unsupported type {spec['type']!r}")
        if "default" not in spec:
            raise SettingsError(f"{where}: missing 'default'")
        normalized[key] = dict(spec)
        _check_value(where, normalized[key], spec["default"])
    return normalized


def _check_value(where: str, spec: Mapping[str, Any], value: Any) -> None:
    expected = _TYPES[spec["type"]]
    # bool is an int subclass; an int setting must not silently accept True.
    if spec["type"] in {"int", "float"} and isinstance(value, bool):
        raise SettingsError(f"{where}: expected {spec['type']}, got bool")
    if not isinstance(value, expected):
        raise SettingsError(f"{where}: expected {spec['type']}, got {type(value).__name__}")
    choices = spec.get("choices")
    if choices is not None and value not in choices:
        raise SettingsError(f"{where}: {value!r} is not one of {list(choices)}")
    minimum, maximum = spec.get("min"), spec.get("max")
    if minimum is not None and value < minimum:
        raise SettingsError(f"{where}: {value!r} is below minimum {minimum}")
    if maximum is not None and value > maximum:
        raise SettingsError(f"{where}: {value!r} is above maximum {maximum}")


class SettingsStore:
    """Schema registry and persistence for every module's settings."""

    def __init__(self, path: Path | str | None = None) -> None:
        default = config_dir() / SETTINGS_FILENAME
        self.path = Path(path).expanduser() if path is not None else default
        self._schemas: dict[str, dict[str, dict[str, Any]]] = {}
        self._values: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SettingsError(f"settings file {self.path} is unreadable: {exc}") from None
        if not isinstance(data, dict):
            raise SettingsError(f"settings file {self.path} must contain an object")
        return {k: dict(v) for k, v in data.items() if isinstance(v, dict)}

    def save(self) -> None:
        payload = json.dumps(self._values, indent=2, sort_keys=True).encode("utf-8")
        write_private_file(self.path, payload)

    def register(self, module_id: str, schema: Mapping[str, Any]) -> SettingsView:
        """Register (or re-register) a module's schema and return its view."""
        self._schemas[module_id] = validate_schema(module_id, schema)
        stored = self._values.get(module_id, {})
        # Drop values whose key vanished from the schema, so a stale file cannot
        # resurrect a removed setting later.
        self._values[module_id] = {k: v for k, v in stored.items() if k in self._schemas[module_id]}
        return self.view(module_id)

    def view(self, module_id: str) -> SettingsView:
        return SettingsView(self, module_id)

    def schema(self, module_id: str) -> dict[str, dict[str, Any]]:
        return dict(self._schemas.get(module_id, {}))

    def schemas(self) -> dict[str, dict[str, dict[str, Any]]]:
        return {k: dict(v) for k, v in self._schemas.items()}

    def is_set(self, module_id: str, key: str) -> bool:
        """Whether a value is *stored*, as opposed to coming from the schema default.

        The distinction is the whole content of "where did this value come from?",
        which is what ``poedex config list`` reports and what makes ``unset``
        meaningful. Nothing else can tell them apart: a stored value equal to the
        default reads identically through :meth:`get`.
        """
        return key in self._values.get(module_id, {})

    def get(self, module_id: str, key: str, default: Any = _UNSET) -> Any:
        """Stored value, else the schema default. Unknown keys raise unless a
        fallback is supplied."""
        spec = self._schemas.get(module_id, {}).get(key)
        if spec is None:
            if default is _UNSET:
                raise UnknownSettingError(f"no setting {module_id}.{key}")
            return default
        return self._values.get(module_id, {}).get(key, spec["default"])

    def set(self, module_id: str, key: str, value: Any) -> None:
        spec = self._schemas.get(module_id, {}).get(key)
        if spec is None:
            raise UnknownSettingError(f"no setting {module_id}.{key}")
        _check_value(f"{module_id}.{key}", spec, value)
        self._values.setdefault(module_id, {})[key] = value
        self.save()

    def reset(self, module_id: str, key: str | None = None) -> None:
        if key is None:
            self._values.pop(module_id, None)
        else:
            self._values.get(module_id, {}).pop(key, None)
        self.save()

    def all(self, module_id: str) -> dict[str, Any]:
        schema = self._schemas.get(module_id, {})
        stored = self._values.get(module_id, {})
        return {key: stored.get(key, spec["default"]) for key, spec in schema.items()}


class SettingsView(Mapping):
    """One module's slice of the store. This is what ``ModuleContext.settings`` is."""

    def __init__(self, store: SettingsStore, module_id: str) -> None:
        self._store = store
        self.module_id = module_id

    def get(self, key: str, default: Any = _UNSET) -> Any:
        return self._store.get(self.module_id, key, default)

    def set(self, key: str, value: Any) -> None:
        self._store.set(self.module_id, key, value)

    def reset(self, key: str | None = None) -> None:
        self._store.reset(self.module_id, key)

    def all(self) -> dict[str, Any]:
        return self._store.all(self.module_id)

    def schema(self) -> dict[str, dict[str, Any]]:
        return self._store.schema(self.module_id)

    def __getitem__(self, key: str) -> Any:
        try:
            return self._store.get(self.module_id, key)
        except UnknownSettingError:
            raise KeyError(key) from None

    def __iter__(self) -> Iterator[str]:
        return iter(self._store.schema(self.module_id))

    def __len__(self) -> int:
        return len(self._store.schema(self.module_id))

    def __repr__(self) -> str:
        return f"SettingsView({self.module_id!r})"
