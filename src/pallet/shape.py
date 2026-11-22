"""Shaping: a small, deterministic transform from captured bytes to typed rows.

Every output value carries a state. Only ``reported`` and ``derived`` values
hold a value; every other state leaves the value empty. A token the contract
says means "suppressed" is never parsed as a number.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pallet.errors import PreconditionError, SpecError
from pallet.spec import Contract, Field
from pallet.store import Store
from pallet.util import atomic_write, canonical_json, now_iso, sha256_bytes

STATES = (
    "reported",
    "derived",
    "suppressed",
    "not_collected",
    "not_applicable",
    "parse_failure",
    "unknown",
)
VALUE_STATES = frozenset({"reported", "derived"})

STATE_MEANINGS = {
    "reported": "The publisher reported this value.",
    "derived": "Pallet computed this value by a rule named in the contract (e.g. the edition).",
    "suppressed": "The publisher withheld the value (for privacy or reliability). It is not zero.",
    "not_collected": "The publisher did not collect this value, or the column is absent "
    "from this edition.",
    "not_applicable": "The value cannot exist for this row (a structural absence).",
    "parse_failure": "The publisher's cell could not be read as the declared type.",
    "unknown": "The cell was empty or held a token whose meaning is not known.",
}

_INT = re.compile(r"^[+-]?\d+$")
_NUM = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")
_TRUE = {"true", "t", "yes", "y", "1"}
_FALSE = {"false", "f", "no", "n", "0"}


def state_column(name: str) -> str:
    return f"{name}__state"


def parse_value(kind: str, text: str, thousands: str | None, fmt: str | None) -> str:
    """Return the normalised text of ``text`` as ``kind``, or raise ValueError."""
    if kind == "string":
        return text
    if kind in ("integer", "number"):
        cleaned = text.replace(thousands, "") if thousands else text
        pattern = _INT if kind == "integer" else _NUM
        if not pattern.match(cleaned):
            raise ValueError(text)
        if kind == "integer":
            return str(int(cleaned))
        return cleaned.lstrip("+")
    if kind == "boolean":
        low = text.lower()
        if low in _TRUE:
            return "true"
        if low in _FALSE:
            return "false"
        raise ValueError(text)
    if kind == "date":
        if fmt:
            return _dt.datetime.strptime(text, fmt).date().isoformat()
        return _dt.date.fromisoformat(text).isoformat()
    raise ValueError(f"unknown type {kind}")


def read_table(data: bytes, contract: Contract) -> tuple[list[str], list[dict[str, str]]]:
    source = contract.source
    encoding = source.get("encoding", "utf-8")
    if encoding.replace("-", "").lower() == "utf8":
        encoding = "utf-8-sig"  # tolerate a byte-order mark
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError as exc:
        named = source.get("encoding", "utf-8")
        raise SpecError(f"cannot decode source as {named}: {exc}") from exc
    fmt = source["format"]
    if fmt == "jsonl":
        return _read_jsonl(text)
    delimiter = "\t" if fmt == "tsv" else source.get("delimiter", ",")
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for _ in range(source.get("skip_rows", 0)):
        next(reader, None)
    header = next(reader, None)
    if header is None:
        raise SpecError("source has no header row")
    header = [h.strip() for h in header]
    rows = []
    for number, cells in enumerate(reader, start=2 + source.get("skip_rows", 0)):
        if not any(c.strip() for c in cells):
            continue
        if len(cells) > len(header) and any(c.strip() for c in cells[len(header):]):
            raise SpecError(f"source row {number} has more cells than the header")
        rows.append(dict(zip(header, cells, strict=False)))
    return header, rows


def _read_jsonl(text: str) -> tuple[list[str], list[dict[str, str]]]:
    header: list[str] = []
    rows = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SpecError(f"source line {number} is not JSON: {exc}") from exc
        if not isinstance(obj, dict):
            raise SpecError(f"source line {number} is not a JSON object")
        row = {}
        for k, v in obj.items():
            if k not in header:
                header.append(k)
            if v is None:
                row[k] = ""
            elif isinstance(v, bool):
                row[k] = "true" if v else "false"
            elif isinstance(v, (int, float)):
                row[k] = json.dumps(v)
            elif isinstance(v, str):
                row[k] = v
            else:
                row[k] = json.dumps(v, sort_keys=True)
        rows.append(row)
    return header, rows


@dataclass
class ShapeResult:
    fields: list[Field]
    rows: list[dict[str, tuple[str, str]]]
    editions: list[dict[str, Any]]
    state_counts: dict[str, dict[str, int]]
    parse_failures: list[dict[str, Any]] = field(default_factory=list)

    @property
    def state_columns(self) -> list[str]:
        return [f.name for f in self.fields
                if any(s != "reported" for s in self.state_counts[f.name])]

    def to_csv(self) -> bytes:
        with_state = set(self.state_columns)
        header: list[str] = []
        for f in self.fields:
            header.append(f.name)
            if f.name in with_state:
                header.append(state_column(f.name))
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        for row in self.rows:
            out = []
            for f in self.fields:
                value, state = row[f.name]
                out.append(value)
                if f.name in with_state:
                    out.append(state)
            writer.writerow(out)
        return buffer.getvalue().encode("utf-8")


def _passes(filters: list[dict[str, Any]], raw: dict[str, str]) -> bool:
    for flt in filters:
        text = raw.get(flt["field"], "")
        op, value = flt["op"], flt["value"]
        if op == "eq" and text != value:
            return False
        if op == "ne" and text == value:
            return False
        if op == "in" and text not in value:
            return False
        if op == "not_in" and text in value:
            return False
    return True


def convert(f: Field, cell: str | None, present: bool, label: str, thousands: str | None):
    """Return (value, state) for one cell."""
    if f.derive == "edition":
        try:
            return parse_value(f.type, label, thousands, f.format), "derived"
        except ValueError:
            return "", "parse_failure"
    if not present:
        return "", "not_collected"
    text = (cell or "").strip()
    for category, tokens in f.tokens.items():
        if text in tokens:
            return "", category
    if text == "":
        return "", "unknown"
    try:
        return parse_value(f.type, text, thousands, f.format), "reported"
    except ValueError:
        return "", "parse_failure"


def shape(contract: Contract, inputs: list[tuple[str, bytes]]) -> ShapeResult:
    """Shape (edition label, bytes) pairs, in edition order, into typed rows."""
    shape_spec = contract.shape
    thousands = shape_spec.get("thousands")
    filters = shape_spec.get("filters", [])
    key = shape_spec.get("key", [])
    counts = {f.name: {} for f in contract.fields}
    rows: list[dict[str, tuple[str, str]]] = []
    editions: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    seen_keys: dict[tuple, str] = {}

    for label, data in inputs:
        header, raw_rows = read_table(data, contract)
        columns: dict[str, str | None] = {}
        for f in contract.fields:
            columns[f.name] = next((s for s in f.sources if s in header), None)
        absent = [f.name for f in contract.fields if not f.derive and columns[f.name] is None]
        kept = 0
        for index, raw in enumerate(raw_rows, start=1):
            texts = {}
            for f in contract.fields:
                col = columns[f.name]
                texts[f.name] = label if f.derive else (raw.get(col) or "").strip() if col else ""
            if not _passes(filters, texts):
                continue
            row = {}
            for f in contract.fields:
                col = columns[f.name]
                cell = raw.get(col) if col else None
                value, state = convert(f, cell, col is not None or bool(f.derive), label,
                                       thousands)
                row[f.name] = (value, state)
                counts[f.name][state] = counts[f.name].get(state, 0) + 1
                if state == "parse_failure" and len(failures) < 20:
                    failures.append({"edition": label, "row": index, "field": f.name,
                                     "literal": (cell or "").strip()})
            if key:
                kv = tuple(row[k][0] for k in key)
                bad = [k for k in key if row[k][1] not in VALUE_STATES]
                if bad:
                    raise SpecError(
                        f"edition {label} row {index}: key field(s) {', '.join(bad)} have no value"
                    )
                if kv in seen_keys:
                    raise SpecError(
                        f"edition {label} row {index}: key {dict(zip(key, kv, strict=True))} "
                        f"already seen in edition {seen_keys[kv]}"
                    )
                seen_keys[kv] = label
            rows.append(row)
            kept += 1
        editions.append({"edition": label, "rows_read": len(raw_rows), "rows_kept": kept,
                         "absent_fields": absent})

    result = ShapeResult(fields=contract.fields, rows=rows, editions=editions,
                         state_counts=counts, parse_failures=failures)
    total_failures = sum(c.get("parse_failure", 0) for c in counts.values())
    if total_failures and not shape_spec.get("allow_parse_failures", False):
        examples = "; ".join(
            f"{p['edition']} row {p['row']} {p['field']}={p['literal']!r}" for p in failures[:5]
        )
        raise SpecError(
            f"{total_failures} cell(s) could not be read as their declared type ({examples}). "
            "Fix the field types or missing-value tokens, or set allow_parse_failures = true.",
            parse_failures=failures,
        )
    return result


def gather_inputs(store: Store, contract: Contract) -> list[dict[str, Any]]:
    """The latest captured receipt for every edition, in contract order."""
    latest = store.latest_receipts(contract.id, "edition")
    out = []
    for edition in contract.editions:
        receipt = latest.get(edition["label"])
        if receipt is None:
            raise PreconditionError(
                f"edition {edition['label']} of {contract.id} has never been captured "
                "(run `pallet capture`)"
            )
        out.append({
            "edition": edition["label"],
            "url": receipt["url"],
            "sha256": receipt["sha256"],
            "bytes": receipt["bytes"],
            "receipt_id": receipt["receipt_id"],
            "retrieved_at": receipt["retrieved_at"],
        })
    return out


def shape_inputs(store: Store, contract: Contract, inputs: list[dict[str, Any]]) -> ShapeResult:
    return shape(contract, [(i["edition"], store.read_raw(i["sha256"])) for i in inputs])


def run_shape(store: Store, dataset_id: str) -> dict[str, Any]:
    contract = store.load_contract(dataset_id)
    store.require_state(dataset_id, ("captured", "shaped", "released"), "shape")
    inputs = gather_inputs(store, contract)
    result = shape_inputs(store, contract, inputs)
    data = result.to_csv()
    summary = {
        "dataset_id": dataset_id,
        "shaped_at": now_iso(),
        "contract_sha256": contract.sha256,
        "inputs": inputs,
        "data_sha256": sha256_bytes(data),
        "row_count": len(result.rows),
        "editions": result.editions,
        "state_counts": result.state_counts,
        "state_columns": result.state_columns,
        "parse_failures": result.parse_failures,
    }
    folder = store.dataset_dir(dataset_id) / "shaped"
    atomic_write(folder / "data.csv", data)
    atomic_write(folder / "shape.json", canonical_json(summary))
    store.record_state(dataset_id, "shaped", data_sha256=summary["data_sha256"],
                       rows=summary["row_count"])
    return summary
