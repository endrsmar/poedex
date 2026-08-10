#!/usr/bin/env python3
"""Generate ``frontend/core/src/types/generated.ts`` from the pydantic models.

IMPLEMENTATION-PLAN §3: *"Pydantic models are the single source of truth for types;
a build step generates TS from their JSON Schema and CI fails if it is stale."*

Two families feed it:

* :mod:`modules.poeapi.backend.models` — the normalized item model, SPEC §4.5's
  boundary, already pydantic.
* :mod:`transports.wire` — the appraisal/prices payloads and the transport
  envelopes, declared as pydantic in the transport because the domain classes they
  mirror are deliberately not (see that module's docstring).

Run with ``--check`` to fail when the checked-in file differs, which is what
``tests/test_generated_types.py`` and ``pnpm run types:check`` do. The check is the
point: a generator nobody runs produces a TS model that agrees with Python only on
the day it was written.

This is a hand-rolled JSON-Schema→TypeScript emitter rather than
``datamodel-code-generator`` or ``json-schema-to-typescript``. It is ~150 lines, it
handles exactly the subset pydantic v2 emits for these models, and it adds no
dependency to a project that has to vendor everything into ``py_modules/`` for the
Decky backend. It raises on any schema construct it does not understand, so a model
that outgrows it fails loudly here rather than emitting ``any``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from modules.poeapi.backend import models as poeapi_models  # noqa: E402
from transports import wire  # noqa: E402

OUTPUT = REPO_ROOT / "frontend" / "core" / "src" / "types" / "generated.ts"

# Roots, in emission order. Everything they reference is pulled in transitively.
ROOTS = (
    poeapi_models.NormalizedItem,
    poeapi_models.ItemSet,
    poeapi_models.CharacterList,
    *wire.WIRE_MODELS,
)

HEADER = """\
/**
 * GENERATED FILE — do not edit.
 *
 * Emitted from the pydantic models' JSON Schema by `scripts/generate_types.py`.
 * `python3 scripts/generate_types.py` rewrites it; `--check` fails when it is
 * stale, which is what `pnpm run types:check` and `tests/test_generated_types.py`
 * run. The Python and TypeScript item models are not allowed to drift, and this
 * file is the mechanism rather than the promise.
 *
 * Sources:
 *   modules/poeapi/backend/models.py   the normalized item model (SPEC §4.5)
 *   transports/wire.py                 the appraisal/prices payloads and envelopes
 */
"""

PRIMITIVES = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
    "null": "null",
}


class UnsupportedSchema(Exception):
    """The emitter met a construct it will not guess about."""


def collect(models: tuple[type, ...]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Flatten every model's schema into one ``name -> schema`` namespace.

    ``mode="serialization"`` on purpose: it is the shape ``to_json``/``model_dump``
    actually produces, so a field with a default is *present* on the wire and its TS
    type is non-optional rather than politely lying with ``?``.
    """
    defs: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for model in models:
        schema = model.model_json_schema(
            ref_template="#/$defs/{model}", mode="serialization"
        )
        nested = schema.pop("$defs", {})
        for name, sub in nested.items():
            _merge(defs, name, sub)
        title = schema.get("title") or model.__name__
        _merge(defs, title, schema)
        if title not in order:
            order.append(title)
    for name in sorted(defs):
        if name not in order:
            order.append(name)
    return defs, order


def _merge(defs: dict[str, dict[str, Any]], name: str, schema: dict[str, Any]) -> None:
    existing = defs.get(name)
    if existing is not None and existing != schema:
        raise UnsupportedSchema(
            f"two different models are both called {name!r}; rename one — a TS "
            "namespace has no packages to keep them apart"
        )
    defs[name] = schema


def ts_type(node: Any) -> str:
    """One JSON Schema node as a TypeScript type expression."""
    if not isinstance(node, dict):
        raise UnsupportedSchema(f"expected an object schema, got {node!r}")
    if "$ref" in node:
        return node["$ref"].rsplit("/", 1)[-1]
    if "const" in node:
        return json.dumps(node["const"])
    if "enum" in node:
        return " | ".join(json.dumps(value) for value in node["enum"])
    if "anyOf" in node or "oneOf" in node:
        parts = [ts_type(sub) for sub in node.get("anyOf") or node["oneOf"]]
        # Keep declaration order but drop duplicates: `str | None` and
        # `Optional[str]` produce the same two branches.
        seen: list[str] = []
        for part in parts:
            if part not in seen:
                seen.append(part)
        return " | ".join(seen)
    kind = node.get("type")
    if kind == "array":
        items = node.get("items")
        if items is None:
            return "unknown[]"
        inner = ts_type(items)
        return f"({inner})[]" if "|" in inner else f"{inner}[]"
    if kind == "object":
        extra = node.get("additionalProperties")
        if "properties" in node:
            return _inline_object(node)
        if extra in (True, None):
            return "Record<string, unknown>"
        return f"Record<string, {ts_type(extra)}>"
    if isinstance(kind, list):
        return " | ".join(PRIMITIVES[k] for k in kind)
    if kind in PRIMITIVES:
        return PRIMITIVES[kind]
    if not node:
        # pydantic emits `{}` for a bare `object`/`Any` annotation.
        return "unknown"
    raise UnsupportedSchema(f"cannot express {node!r} in TypeScript")


def _inline_object(node: dict[str, Any]) -> str:
    required = set(node.get("required", ()))
    fields = [
        f"{json.dumps(name)}{'' if name in required else '?'}: {ts_type(sub)}"
        for name, sub in node["properties"].items()
    ]
    return "{ " + "; ".join(fields) + " }"


def emit(name: str, schema: dict[str, Any]) -> str:
    doc = _docblock(schema.get("description"), indent="")
    if "enum" in schema and "properties" not in schema:
        return f"{doc}export type {name} = {ts_type(schema)}\n"
    if schema.get("type") != "object" or "properties" not in schema:
        return f"{doc}export type {name} = {ts_type(schema)}\n"
    required = set(schema.get("required", ()))
    lines = [f"{doc}export interface {name} {{"]
    for field, sub in schema["properties"].items():
        field_doc = _docblock(sub.get("description"), indent="  ")
        optional = "" if field in required else "?"
        lines.append(f"{field_doc}  {field}{optional}: {ts_type(sub)}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _docblock(description: str | None, *, indent: str) -> str:
    if not description:
        return ""
    body = " ".join(description.split())
    wrapped: list[str] = []
    line = ""
    for word in body.split(" "):
        if line and len(line) + len(word) + 1 > 76:
            wrapped.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        wrapped.append(line)
    inner = "\n".join(f"{indent} * {text}" for text in wrapped)
    return f"{indent}/**\n{inner}\n{indent} */\n"


def render() -> str:
    defs, order = collect(ROOTS)
    blocks = [emit(name, defs[name]) for name in order]
    return HEADER + "\n" + "\n".join(blocks)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the checked-in file is stale",
    )
    args = parser.parse_args(argv)
    generated = render()
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current == generated:
            return 0
        print(
            f"{OUTPUT.relative_to(REPO_ROOT)} is stale — the pydantic models and the "
            "TypeScript types have drifted.\nRun: python3 scripts/generate_types.py",
            file=sys.stderr,
        )
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(generated, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
