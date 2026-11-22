"""Validate documents against pallet's shipped JSON Schemas.

The schemas are ordinary JSON Schema 2020-12 files, so any full validator can
use them. To keep pallet free of runtime dependencies this module implements
the subset of keywords the schemas use (``SUPPORTED_KEYWORDS``); a test checks
that the schemas use nothing else and that a full validator agrees.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from functools import cache
from importlib import resources
from typing import Any

from pallet.errors import SpecError

SCHEMA_NAMES = ("contract", "receipt", "manifest", "pin")

SUPPORTED_KEYWORDS = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "$comment",
        "title",
        "description",
        "type",
        "enum",
        "const",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "minItems",
        "uniqueItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "anyOf",
        "default",
        "examples",
    }
)


@cache
def load_schema(name: str) -> dict[str, Any]:
    if name not in SCHEMA_NAMES:
        raise KeyError(name)
    text = resources.files("pallet").joinpath("schemas", f"{name}.schema.json").read_text("utf-8")
    return json.loads(text)


def _is_type(value: Any, kind: str) -> bool:
    if kind == "object":
        return isinstance(value, dict)
    if kind == "array":
        return isinstance(value, list)
    if kind == "string":
        return isinstance(value, str)
    if kind == "boolean":
        return isinstance(value, bool)
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == "null":
        return value is None
    raise ValueError(f"unsupported type {kind!r}")


def _resolve(ref: str, root: dict[str, Any]) -> Any:
    if not ref.startswith("#/"):
        raise ValueError(f"only local references are supported: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def iter_errors(
    instance: Any, schema: Any, root: dict[str, Any] | None = None, path: str = "$"
) -> Iterator[str]:
    root = schema if root is None else root
    if schema is True:
        return
    if schema is False:
        yield f"{path}: not allowed"
        return
    if "$ref" in schema:
        yield from iter_errors(instance, _resolve(schema["$ref"], root), root, path)
    if "type" in schema:
        kinds = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(instance, k) for k in kinds):
            yield f"{path}: expected {' or '.join(kinds)}, got {type(instance).__name__}"
            return
    if "const" in schema and instance != schema["const"]:
        yield f"{path}: must be {schema['const']!r}"
    if "enum" in schema and instance not in schema["enum"]:
        options = ", ".join(repr(v) for v in schema["enum"])
        yield f"{path}: {instance!r} is not one of {options}"
    if "anyOf" in schema:
        if not any(not list(iter_errors(instance, s, root, path)) for s in schema["anyOf"]):
            yield f"{path}: does not match any allowed form"
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            yield f"{path}: shorter than {schema['minLength']} characters"
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            yield f"{path}: longer than {schema['maxLength']} characters"
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            yield f"{path}: {instance!r} does not match {schema['pattern']}"
    if _is_type(instance, "number") and "minimum" in schema and instance < schema["minimum"]:
        yield f"{path}: less than {schema['minimum']}"
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            yield f"{path}: needs at least {schema['minItems']} item(s)"
        if schema.get("uniqueItems"):
            seen = [json.dumps(v, sort_keys=True) for v in instance]
            if len(seen) != len(set(seen)):
                yield f"{path}: items must be unique"
        if "items" in schema:
            for index, item in enumerate(instance):
                yield from iter_errors(item, schema["items"], root, f"{path}[{index}]")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                yield f"{path}: missing required key {key!r}"
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            sub = f"{path}.{key}"
            if key in props:
                yield from iter_errors(value, props[key], root, sub)
            elif extra is False:
                yield f"{path}: unexpected key {key!r}"
            elif isinstance(extra, dict):
                yield from iter_errors(value, extra, root, sub)


def errors(instance: Any, name: str, ref: str | None = None) -> list[str]:
    root = load_schema(name)
    schema = _resolve(ref, root) if ref else root
    return list(iter_errors(instance, schema, root))


def validate(instance: Any, name: str, ref: str | None = None, what: str | None = None) -> None:
    problems = errors(instance, name, ref)
    if problems:
        label = what or name
        raise SpecError(f"{label} is not valid: " + "; ".join(problems[:8]), problems=problems)
