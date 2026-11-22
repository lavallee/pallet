"""Frictionless Data Package and plain-language README generation and checking."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from pallet import TOOL
from pallet.shape import STATE_MEANINGS, STATES, parse_value, state_column
from pallet.spec import Contract
from pallet.util import sha256_file

FRICTIONLESS_TYPES = {"string", "integer", "number", "boolean", "date"}
MISSING_VALUES = [""]
NOT_FOR_REDISTRIBUTION = (
    "NOT FOR REDISTRIBUTION. This copy was produced with --private because the dataset's "
    "contract does not allow redistribution. Do not share or publish it."
)


def build_datapackage(
    contract: Contract,
    *,
    release_id: str,
    created_at: str,
    data_sha256: str,
    data_bytes: int,
    row_count: int,
    state_columns: list[str],
    state_counts: dict[str, dict[str, int]],
    inputs: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    private: bool = False,
) -> dict[str, Any]:
    ds, rights = contract.dataset, contract.rights
    with_state = set(state_columns)
    fields: list[dict[str, Any]] = []
    for f in contract.fields:
        entry: dict[str, Any] = {"name": f.name, "type": f.type}
        if f.title:
            entry["title"] = f.title
        if f.description:
            entry["description"] = f.description
        if f.unit:
            entry["unit"] = f.unit
        if f.derive:
            entry["pallet:derived_from"] = f.derive
        if f.type == "date":
            entry["format"] = "default"
        fields.append(entry)
        if f.name in with_state:
            fields.append({
                "name": state_column(f.name),
                "type": "string",
                "description": f"Value state of `{f.name}`. Only `reported` and `derived` rows "
                f"carry a value; for every other state `{f.name}` is empty.",
                "constraints": {"required": True, "enum": list(STATES)},
            })
    schema: dict[str, Any] = {"fields": fields, "missingValues": list(MISSING_VALUES)}
    key = contract.shape.get("key", [])
    if key:
        schema["primaryKey"] = list(key)
    licence: dict[str, Any] = {"name": rights["license"]}
    if rights.get("license_url"):
        licence["path"] = rights["license_url"]
    package: dict[str, Any] = {
        "profile": "tabular-data-package",
        "name": contract.id,
        "title": ds["title"],
        "description": ds["description"],
        "version": release_id,
        "created": created_at,
        "licenses": [licence],
        "sources": [
            {"title": f"{ds['publisher']}, edition {i['edition']}", "path": i["url"]}
            for i in inputs
        ],
        "resources": [{
            "name": "data",
            "path": "data.csv",
            "profile": "tabular-data-resource",
            "format": "csv",
            "mediatype": "text/csv",
            "encoding": "utf-8",
            "bytes": data_bytes,
            "hash": f"sha256:{data_sha256}",
            "schema": schema,
        }],
        "pallet": {
            "tool": TOOL,
            "dataset_id": contract.id,
            "release_id": release_id,
            "row_count": row_count,
            "publisher": ds["publisher"],
            "cadence": ds["cadence"],
            "geography": ds.get("geography", ""),
            "time_basis": ds.get("time_basis", ""),
            "grain": ds.get("grain", ""),
            "rights": rights,
            "not_for_redistribution": private,
            "value_states": STATE_MEANINGS,
            "state_columns": [state_column(n) for n in state_columns],
            "state_counts": state_counts,
            "inputs": inputs,
            "issues": issues,
        },
    }
    if ds.get("homepage"):
        package["homepage"] = ds["homepage"]
    if ds.get("keywords"):
        package["keywords"] = ds["keywords"]
    return package


def _para(text: str) -> str:
    return " ".join(text.split())


def render_readme(contract: Contract, package: dict[str, Any], private: bool = False) -> str:
    ds, rights, meta = contract.dataset, contract.rights, package["pallet"]
    out: list[str] = []
    out.append(f"# {ds['title']}\n")
    if private:
        out.append(f"> **{NOT_FOR_REDISTRIBUTION}**\n")
    out.append(_para(ds["description"]) + "\n")
    out.append(f"- Dataset: `{contract.id}`, release `{meta['release_id']}`")
    out.append(f"- Publisher: {ds['publisher']}" + (f" ({ds['publisher_url']})"
                                                     if ds.get("publisher_url") else ""))
    out.append(f"- Published: {ds['cadence']}")
    for label, key in (("Each row", "grain"), ("Geography", "geography"),
                       ("Time basis", "time_basis")):
        if ds.get(key):
            out.append(f"- {label}: {ds[key]}")
    out.append(f"- Rows: {meta['row_count']}\n")

    out.append("## Rights\n")
    out.append(f"- Licence: {rights['license']}"
               + (f" ({rights['license_url']})" if rights.get("license_url") else ""))
    redistribution = {
        "allowed": "allowed",
        "restricted": "restricted to: " + ", ".join(rights.get("redistribute_to", [])),
        "prohibited": "not allowed",
        "unknown": "not yet determined; treat as not allowed",
    }[rights["redistribution"]]
    out.append(f"- Redistribution: {redistribution}")
    if rights.get("attribution"):
        out.append(f"- Credit: {rights['attribution']}")
    for use in rights.get("allowed_uses", []):
        out.append(f"- Allowed use: {use}")
    for use in rights.get("prohibited_uses", []):
        out.append(f"- Prohibited use: {use}")
    if rights.get("notes"):
        out.append(f"- Note: {_para(rights['notes'])}")
    out.append("")

    issues = meta["issues"]
    out.append("## Known issues\n")
    if not issues:
        out.append("None recorded. That is not the same as none existing.\n")
    for issue in issues:
        severity = issue.get("severity", "caution")
        out.append(f"### {issue['title']} ({severity})\n")
        out.append(_para(issue["effect"]) + "\n")
        if issue.get("misuse"):
            out.append(f"- Invites this mistake: {_para(issue['misuse'])}")
        if issue.get("instead"):
            out.append(f"- Instead: {_para(issue['instead'])}")
        if issue.get("fields"):
            out.append(f"- Fields: {', '.join(issue['fields'])}")
        if issue.get("editions"):
            out.append(f"- Editions: {', '.join(issue['editions'])}")
        if issue.get("publisher_quote"):
            cite = ", ".join(x for x in (issue.get("quote_source"), issue.get("quote_date")) if x)
            out.append(f"- The publisher says: \"{_para(issue['publisher_quote'])}\""
                       + (f" ({cite})" if cite else ""))
        if issue.get("recorded"):
            out.append(f"- Recorded: {issue['recorded']}")
        out.append("")

    out.append("## Fields\n")
    out.append("| Field | Type | Unit | Description |")
    out.append("| --- | --- | --- | --- |")
    for f in package["resources"][0]["schema"]["fields"]:
        desc = f.get("description", "").replace("|", "\\|")
        out.append(f"| `{f['name']}` | {f['type']} | {f.get('unit', '')} | {_para(desc)} |")
    out.append("")

    out.append("## Missing values\n")
    out.append(
        "A blank cell is never a zero. Where a field has any value that was not simply "
        "reported, a companion column named `<field>__state` says why, row by row:\n"
    )
    for state in STATES:
        out.append(f"- `{state}`: {STATE_MEANINGS[state]}")
    out.append("")
    counted = [(name, c) for name, c in meta["state_counts"].items()
               if any(s != "reported" for s in c)]
    if counted:
        out.append("| Field | " + " | ".join(STATES) + " |")
        out.append("| --- |" + " --- |" * len(STATES))
        for name, c in counted:
            out.append(f"| `{name}` | " + " | ".join(str(c.get(s, 0)) for s in STATES) + " |")
        out.append("")

    out.append("## Where this came from\n")
    for i in meta["inputs"]:
        out.append(f"- Edition {i['edition']}: {i['url']}, retrieved {i['retrieved_at']}, "
                   f"{i['bytes']} bytes, sha256 `{i['sha256']}`")
    out.append("")
    out.append(
        "The release's `manifest.json` lists the SHA-256 of every file here. To cite a number "
        "from this table, record a pin (`pallet pin`) so it can be traced to this exact release.\n"
    )
    return "\n".join(out)


def _cell_ok(kind: str, text: str) -> bool:
    try:
        parse_value(kind, text, None, None)
        return True
    except ValueError:
        return False


def check_datapackage(folder: Path) -> list[str]:
    """Structural checks on a Frictionless tabular data package. Returns problems found."""
    problems: list[str] = []
    try:
        package = json.loads((folder / "datapackage.json").read_text("utf-8"))
    except FileNotFoundError:
        return ["datapackage.json is missing"]
    except json.JSONDecodeError as exc:
        return [f"datapackage.json is not JSON: {exc}"]
    if not isinstance(package.get("name"), str) or not package["name"]:
        problems.append("package needs a name")
    for licence in package.get("licenses", []):
        if not (licence.get("name") or licence.get("path")):
            problems.append("each licence needs a name or path")
    resources = package.get("resources")
    if not isinstance(resources, list) or not resources:
        return problems + ["package needs at least one resource"]
    for res in resources:
        name = res.get("name", "?")
        path = res.get("path")
        if not isinstance(path, str) or path.startswith("/") or ".." in Path(path).parts:
            problems.append(f"resource {name}: path must be a relative path inside the package")
            continue
        file = folder / path
        if not file.is_file():
            problems.append(f"resource {name}: {path} is missing")
            continue
        if "bytes" in res and res["bytes"] != file.stat().st_size:
            problems.append(f"resource {name}: bytes does not match {path}")
        if "hash" in res and res["hash"] != f"sha256:{sha256_file(file)}":
            problems.append(f"resource {name}: hash does not match {path}")
        schema = res.get("schema", {})
        fields = schema.get("fields", [])
        names = [f.get("name") for f in fields]
        if len(names) != len(set(names)):
            problems.append(f"resource {name}: field names must be unique")
        for f in fields:
            if f.get("type") not in FRICTIONLESS_TYPES:
                problems.append(f"resource {name}: field {f.get('name')} has type {f.get('type')}")
        missing = schema.get("missingValues", [""])
        if not isinstance(missing, list) or not all(isinstance(m, str) for m in missing):
            problems.append(f"resource {name}: missingValues must be a list of strings")
            missing = [""]
        for k in schema.get("primaryKey", []):
            if k not in names:
                problems.append(f"resource {name}: primary key field {k} is not a field")
        reader = csv.reader(io.StringIO(file.read_text("utf-8"), newline=""))
        header = next(reader, [])
        if header != names:
            problems.append(f"resource {name}: CSV header does not match the schema fields")
            continue
        types = [f["type"] for f in fields]
        enums = [f.get("constraints", {}).get("enum") for f in fields]
        for number, row in enumerate(reader, start=2):
            if len(row) != len(names):
                problems.append(f"resource {name}: row {number} has {len(row)} cells")
                continue
            for cell, kind, enum, fname in zip(row, types, enums, names, strict=True):
                if cell in missing:
                    continue
                if enum is not None and cell not in enum:
                    problems.append(f"resource {name}: row {number} {fname}={cell!r} not allowed")
                elif not _cell_ok(kind, cell):
                    problems.append(f"resource {name}: row {number} {fname}={cell!r} is not {kind}")
            if len(problems) > 50:
                return problems
    return problems
