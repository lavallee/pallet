"""Small helpers: hashing, canonical JSON, atomic writes, time."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

_CHUNK = 1 << 20

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Stable, human-readable JSON: sorted keys, two-space indent, trailing newline."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def compact_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.UTC).replace(microsecond=0)


def iso(moment: _dt.datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(utc_now())


def today() -> str:
    return utc_now().date().isoformat()


def fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so a reader sees the old bytes or the new, never a mix."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    fsync_dir(path.parent)


def append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line.rstrip("\n") + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: Path) -> list[Any]:
    if not path.exists():
        return []
    out = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def normalize_toml(value: Any) -> Any:
    """Turn TOML dates and times into ISO strings so documents are plain JSON."""
    if isinstance(value, dict):
        return {k: normalize_toml(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize_toml(v) for v in value]
    if isinstance(value, (_dt.date, _dt.datetime, _dt.time)):
        return value.isoformat()
    return value


def slug(text: str, limit: int = 60) -> str:
    out = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return out[:limit].strip("-") or "item"
