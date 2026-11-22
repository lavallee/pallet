"""Load and check dataset specs (contracts) and issues."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pallet import validation
from pallet.errors import SpecError
from pallet.util import normalize_toml, sha256_bytes

MISSING_CATEGORIES = ("suppressed", "not_collected", "not_applicable", "unknown")
REDISTRIBUTABLE = "allowed"


@dataclass(frozen=True)
class Field:
    name: str
    type: str
    sources: tuple[str, ...]
    derive: str | None
    unit: str
    description: str
    title: str
    format: str | None
    tokens: dict[str, frozenset[str]]


@dataclass
class Contract:
    """A validated spec. ``text`` is the exact bytes registered, kept for custody."""

    doc: dict[str, Any]
    text: str
    fields: list[Field] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.doc["dataset"]["id"]

    @property
    def dataset(self) -> dict[str, Any]:
        return self.doc["dataset"]

    @property
    def rights(self) -> dict[str, Any]:
        return self.doc["rights"]

    @property
    def source(self) -> dict[str, Any]:
        return self.doc["source"]

    @property
    def shape(self) -> dict[str, Any]:
        return self.doc["shape"]

    @property
    def editions(self) -> list[dict[str, Any]]:
        return self.doc["source"]["editions"]

    @property
    def issues(self) -> list[dict[str, Any]]:
        return self.doc.get("issues", [])

    @property
    def is_draft(self) -> bool:
        return bool(self.doc["pallet"].get("draft", False))

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.text.encode("utf-8"))

    @property
    def redistributable(self) -> bool:
        return self.rights["redistribution"] == REDISTRIBUTABLE

    def line_of(self, table: str, key: str) -> int | None:
        return find_line(self.text, table, key)


def find_line(text: str, table: str, key: str) -> int | None:
    """1-based line number of ``key = ...`` inside ``[table]``, or None."""
    current = ""
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        header = re.match(r"^\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$", stripped)
        if header:
            current = header.group(1)
            continue
        if current == table and key_re.match(line):
            return number
    return None


def _as_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def parse_contract(text: str, origin: str = "spec") -> Contract:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{origin}: not valid TOML: {exc}") from exc
    doc = normalize_toml(raw)
    validation.validate(doc, "contract", what=origin)
    contract = Contract(doc=doc, text=text)
    contract.fields = _check_semantics(contract, origin)
    return contract


def load_contract(path: Path) -> Contract:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SpecError(f"spec not found: {path}") from exc
    return parse_contract(text, origin=path.name)


def _tokens(base: dict[str, list[str]], override: dict[str, list[str]] | None, where: str):
    merged = {cat: list(base.get(cat, [])) for cat in MISSING_CATEGORIES}
    for cat, values in (override or {}).items():
        merged[cat] = list(values)
    seen: dict[str, str] = {}
    for cat in MISSING_CATEGORIES:
        for token in merged[cat]:
            token = token.strip()
            if token in seen and seen[token] != cat:
                raise SpecError(
                    f"{where}: token {token!r} is listed as both {seen[token]} and {cat}"
                )
            seen[token] = cat
    return {cat: frozenset(t.strip() for t in merged[cat]) for cat in MISSING_CATEGORIES}


def _check_semantics(contract: Contract, origin: str) -> list[Field]:
    doc = contract.doc
    labels = [e["label"] for e in contract.editions]
    if len(labels) != len(set(labels)):
        raise SpecError(f"{origin}: edition labels must be unique")
    for edition in contract.editions:
        scheme = urlparse(edition["url"]).scheme
        if scheme not in ("", "file", "http", "https"):
            raise SpecError(
                f"{origin}: edition {edition['label']}: unsupported URL scheme {scheme}"
            )
    if contract.source["format"] != "csv" and "delimiter" in contract.source:
        raise SpecError(f"{origin}: delimiter applies only to format = \"csv\"")
    rights = contract.rights
    if rights["redistribution"] == "restricted" and not rights.get("redistribute_to"):
        raise SpecError(f"{origin}: redistribution = \"restricted\" needs redistribute_to")

    shape = contract.shape
    base_tokens = shape.get("missing", {})
    fields: list[Field] = []
    names: set[str] = set()
    for spec in shape["fields"]:
        name = spec["name"]
        if name in names:
            raise SpecError(f"{origin}: field {name!r} is defined twice")
        names.add(name)
        if spec.get("derive") and spec.get("source"):
            raise SpecError(f"{origin}: field {name!r} has both source and derive")
        sources = () if spec.get("derive") else (_as_tuple(spec.get("source")) or (name,))
        fields.append(
            Field(
                name=name,
                type=spec["type"],
                sources=sources,
                derive=spec.get("derive"),
                unit=spec.get("unit", ""),
                description=spec.get("description", ""),
                title=spec.get("title", ""),
                format=spec.get("format"),
                tokens=_tokens(base_tokens, spec.get("missing"), f"{origin}: field {name!r}"),
            )
        )
    for key in shape.get("key", []):
        if key not in names:
            raise SpecError(f"{origin}: key field {key!r} is not a defined field")
    for flt in shape.get("filters", []):
        if flt["field"] not in names:
            raise SpecError(f"{origin}: filter field {flt['field']!r} is not a defined field")
        if flt["op"] in ("in", "not_in") and not isinstance(flt["value"], list):
            raise SpecError(f"{origin}: filter op {flt['op']} needs a list value")
        if flt["op"] in ("eq", "ne") and not isinstance(flt["value"], str):
            raise SpecError(f"{origin}: filter op {flt['op']} needs a string value")
    ids = [issue["id"] for issue in doc.get("issues", [])]
    if len(ids) != len(set(ids)):
        raise SpecError(f"{origin}: issue ids must be unique")
    return fields


def parse_issues_file(text: str, origin: str = "issues.toml") -> list[dict[str, Any]]:
    try:
        doc = normalize_toml(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exc:
        raise SpecError(f"{origin}: not valid TOML: {exc}") from exc
    issues = doc.get("issues", [])
    if not isinstance(issues, list):
        raise SpecError(f"{origin}: `issues` must be an array of tables")
    for index, issue in enumerate(issues):
        validate_issue(issue, f"{origin} issue #{index + 1}")
    return issues


def validate_issue(issue: dict[str, Any], what: str = "issue") -> None:
    validation.validate(issue, "contract", ref="#/$defs/issue", what=what)
