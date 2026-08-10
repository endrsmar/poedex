"""``poedex config`` — read and write the settings the rest of the tool talks about.

This command exists because three separate messages told the user to *set a
setting* and there was no way to set one. ``net`` logged "set it with the
net.contact setting"; ``poeapi`` raised "set the poeapi.account setting"; the same
for ``poeapi.league``. All of them named a file nobody had been shown
(``~/.config/poedex/settings.json``) and a key nobody could discover. An instruction
that cannot be followed is worse than silence: it tells the user the tool is
configurable and then makes them read the source to find out how.

Everything here is driven by the schema registry (:mod:`runtime.settings`), so
``list`` is complete by construction — a module that adds a setting gets it listed,
labelled and validated without touching this file.

**Nothing here can print a credential.** The POESESSID does not live in the settings
store at all: `credentials` keeps it in its own ``session.json`` and registers
exactly one setting, an integer age threshold. So the value is not reachable through
a schema in the first place. Two further belts on top of that: this command only
ever reads keys the registry declares — never the raw file, which could hold a
hand-edited leftover — and every value it prints goes through :func:`redact`, which
substitutes out any registered secret and anything shaped like a POESESSID.
"""

from __future__ import annotations

import difflib
import json
import sys
import textwrap
from typing import Any

from runtime.errors import SettingsError, UnknownSettingError
from runtime.registry import Registry
from runtime.secrets import redact
from runtime.settings import SettingsStore

__all__ = ["cmd_config", "format_value", "parse_value", "split_key"]

# Accepted spellings for a boolean. Deliberately generous on input and exact on
# output: `poedex config get` prints JSON, so what it shows can be pasted back.
_TRUE = {"1", "true", "t", "yes", "y", "on"}
_FALSE = {"0", "false", "f", "no", "n", "off"}

_KEY_WIDTH = 26
_VALUE_WIDTH = 24
_WRAP = 78


def split_key(dotted: str) -> tuple[str, str]:
    """``"net.contact"`` → ``("net", "contact")``.

    Split on the *first* dot: module ids never contain one, and this way a key that
    somehow does is still addressable.
    """
    module_id, dot, key = dotted.partition(".")
    if not dot or not module_id or not key:
        raise SettingsError(
            f"{dotted!r} is not a setting name: write it as <module>.<key>, "
            "for example net.contact. 'poedex config list' prints every one."
        )
    return module_id, key


def format_value(value: Any) -> str:
    """A value as it would be typed back in — and never a secret.

    JSON rather than ``repr``: it makes the empty string visible as ``""`` rather
    than as nothing, it distinguishes ``0`` from ``"0"`` and ``false``, and it is
    exactly what ``config set`` accepts for a list or a dict.
    """
    return redact(json.dumps(value, sort_keys=True))


def parse_value(where: str, spec: dict[str, Any], raw: str) -> Any:
    """Turn one argv string into a value of the type the schema declares.

    argv has no types, so this is where ``"14"`` becomes ``14`` and ``"no"`` becomes
    ``False``. Range, choice and type checks are *not* done here: they belong to
    :meth:`SettingsStore.set`, which is also what the frontend and the modules go
    through. One validator, one set of rules.
    """
    kind = spec["type"]
    if kind == "str":
        return raw
    if kind == "bool":
        lowered = raw.strip().casefold()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise SettingsError(f"{where}: {raw!r} is not a boolean — write true or false")
    if kind == "int":
        try:
            return int(raw.strip(), 10)
        except ValueError:
            raise SettingsError(f"{where}: {raw!r} is not a whole number") from None
    if kind == "float":
        try:
            return float(raw.strip())
        except ValueError:
            raise SettingsError(f"{where}: {raw!r} is not a number") from None
    # list and dict: JSON, because there is no other unambiguous way to write a
    # nested value on a command line.
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        example = '\'["Sadist Garb", "Vaal Regalia"]\'' if kind == "list" else '\'{"key": 1}\''
        raise SettingsError(
            f"{where}: {raw!r} is not valid JSON ({exc.msg}). A {kind} is written as "
            f"JSON, for example {example}"
        ) from None
    return parsed


def _spec(store: SettingsStore, dotted: str) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``module.key`` against the registry, or explain what is wrong."""
    module_id, key = split_key(dotted)
    schemas = store.schemas()
    if module_id not in schemas:
        known = ", ".join(sorted(schemas)) or "none — no module registered a schema"
        raise _unknown(schemas, dotted, f"no module {module_id!r}. Registered: {known}.")
    if key not in schemas[module_id]:
        raise _unknown(schemas, dotted, f"no setting {dotted!r}.")
    return module_id, key, schemas[module_id][key]


def _unknown(
    schemas: dict[str, dict[str, dict[str, Any]]], dotted: str, detail: str
) -> UnknownSettingError:
    """The error for a key nobody declares, with a guess and a way to look it up."""
    names = [f"{module_id}.{key}" for module_id, schema in schemas.items() for key in schema]
    close = difflib.get_close_matches(dotted, sorted(names), n=3, cutoff=0.5)
    hint = f" Did you mean {', '.join(close)}?" if close else ""
    return UnknownSettingError(f"{detail}{hint} Run 'poedex config list' to see every setting.")


def _constraints(spec: dict[str, Any]) -> str:
    parts: list[str] = []
    if spec.get("choices") is not None:
        parts.append("one of " + ", ".join(format_value(c) for c in spec["choices"]))
    if spec.get("min") is not None:
        parts.append(f"minimum {spec['min']}")
    if spec.get("max") is not None:
        parts.append(f"maximum {spec['max']}")
    return ", ".join(parts)


def _describe(text: str, indent: str) -> list[str]:
    return textwrap.wrap(" ".join(text.split()), width=_WRAP, initial_indent=indent,
                         subsequent_indent=indent)


def _list(store: SettingsStore, names: dict[str, str], *, verbose: bool) -> int:
    schemas = store.schemas()
    print(f"file:  {store.path}" + ("" if store.path.exists() else "  (not written yet)"))
    if not schemas:
        print("no module registered any settings")
        return 0
    for module_id in sorted(schemas):
        print(f"\n{module_id}  ({names.get(module_id, module_id)})")
        if not schemas[module_id]:
            print("  nothing to configure")
        for key, spec in sorted(schemas[module_id].items()):
            value = store.get(module_id, key)
            stored = store.is_set(module_id, key)
            origin = "set" if stored else "default"
            line = f"  {key:<{_KEY_WIDTH}} {format_value(value):<{_VALUE_WIDTH}} {origin:<8}"
            if spec.get("label"):
                line += f" {spec['label']}"
            if stored:
                line += f"  (default {format_value(spec['default'])})"
            print(line.rstrip())
            if verbose and spec.get("description"):
                for wrapped in _describe(spec["description"], " " * (_KEY_WIDTH + 4)):
                    print(wrapped)
    print("\npoedex config get <module>.<key>          one setting in full")
    print("poedex config set <module>.<key> <value>  store a value")
    print("poedex config unset <module>.<key>        back to the default")
    return 0


def _get(store: SettingsStore, dotted: str) -> int:
    module_id, key, spec = _spec(store, dotted)
    value = store.get(module_id, key)
    stored = store.is_set(module_id, key)
    print(f"setting:     {module_id}.{key}")
    if spec.get("label"):
        print(f"label:       {spec['label']}")
    print(f"type:        {spec['type']}")
    print(f"value:       {format_value(value)}")
    print(f"default:     {format_value(spec['default'])}")
    print(f"source:      {'stored in ' + str(store.path) if stored else 'the schema default'}")
    constraints = _constraints(spec)
    if constraints:
        print(f"constraints: {constraints}")
    if spec.get("description"):
        print("")
        for wrapped in _describe(spec["description"], "  "):
            print(wrapped)
    return 0


def _set(store: SettingsStore, dotted: str, raw: str) -> int:
    module_id, key, spec = _spec(store, dotted)
    where = f"{module_id}.{key}"
    value = parse_value(where, spec, raw)
    previous = store.get(module_id, key)
    was_set = store.is_set(module_id, key)
    # The store validates type, choices, min and max, and writes the file 0600.
    store.set(module_id, key, value)
    print(f"{where} = {format_value(value)}")
    origin = "was" if was_set else "was the default"
    print(f"  ({origin} {format_value(previous)}; stored in {store.path})")
    return 0


def _unset(store: SettingsStore, dotted: str) -> int:
    module_id, key, spec = _spec(store, dotted)
    where = f"{module_id}.{key}"
    if not store.is_set(module_id, key):
        print(f"{where} is already at its default, {format_value(spec['default'])}")
        return 0
    store.reset(module_id, key)
    print(f"{where} = {format_value(spec['default'])}  (back to the default)")
    return 0


async def cmd_config(
    registry: Registry,
    *,
    action: str,
    key: str | None = None,
    value: str | None = None,
    verbose: bool = False,
) -> int:
    """Dispatch one ``poedex config`` subcommand.

    A bad key or a bad value is a *usage* answer, not a crash: it prints what is
    wrong (and, for a near miss, what was probably meant) and exits 1.
    """
    store = registry.settings
    names = {info["id"]: info["name"] for info in registry.status().values()}
    try:
        if action == "list":
            return _list(store, names, verbose=verbose)
        if action == "get":
            return _get(store, str(key))
        if action == "set":
            return _set(store, str(key), "" if value is None else value)
        return _unset(store, str(key))
    except SettingsError as exc:
        # SettingsError covers UnknownSettingError too. redact() because the message
        # can quote the value the user typed, and a user who pasted a POESESSID into
        # the wrong command must not get it echoed back at them.
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 1
