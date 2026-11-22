"""A minimal TOML writer for the documents pallet writes (issues, draft specs).

The standard library reads TOML but does not write it. Pallet only needs to
write strings, integers, floats, booleans, lists of those, tables, and arrays
of tables, so that is all this does.
"""

from __future__ import annotations

import json
import re
from typing import Any

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def key(name: str) -> str:
    return name if _BARE_KEY.match(name) else json.dumps(name, ensure_ascii=False)


def value(obj: Any) -> str:
    if isinstance(obj, bool):
        return "true" if obj else "false"
    if isinstance(obj, int):
        return str(obj)
    if isinstance(obj, float):
        return repr(obj)
    if isinstance(obj, str):
        # JSON string escapes are a subset of TOML basic-string escapes.
        return json.dumps(obj, ensure_ascii=False)
    if isinstance(obj, (list, tuple)):
        return "[" + ", ".join(value(v) for v in obj) + "]"
    if isinstance(obj, dict):
        return "{ " + ", ".join(f"{key(k)} = {value(v)}" for k, v in obj.items()) + " }"
    raise TypeError(f"cannot write {type(obj).__name__} as TOML")


def _is_table_array(obj: Any) -> bool:
    return isinstance(obj, list) and bool(obj) and all(isinstance(v, dict) for v in obj)


def dumps(doc: dict[str, Any], _prefix: str = "") -> str:
    lines: list[str] = []
    nested: list[tuple[str, Any]] = []
    for name, item in doc.items():
        if item is None:
            continue
        if isinstance(item, dict) or _is_table_array(item):
            nested.append((name, item))
        else:
            lines.append(f"{key(name)} = {value(item)}")
    out = "\n".join(lines)
    for name, item in nested:
        full = f"{_prefix}{key(name)}"
        if isinstance(item, dict):
            block = f"[{full}]\n" + dumps(item, full + ".")
        else:
            block = "\n".join(f"[[{full}]]\n" + dumps(entry, full + ".") for entry in item)
        out = (out + "\n\n" if out else "") + block.rstrip("\n")
    return out + "\n" if out else ""
