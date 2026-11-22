"""The quick path: capture one table and infer a draft spec for a person to review.

Adopt never describes, shapes, or releases anything. It writes a draft spec
marked ``draft = true``; ``pallet describe`` refuses it until a person has
reviewed every line marked REVIEW and removed the flag.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pallet import TOOL
from pallet.capture import retrieve
from pallet.errors import PreconditionError, SpecError
from pallet.shape import parse_value
from pallet.spec import parse_contract
from pallet.store import Store
from pallet.tomlw import value as tv
from pallet.util import today

SUPPRESSED_HINTS = {"*", "**", "***", "s", "(s)", "x", "(x)", "d", "(d)", "suppressed",
                    "redacted", "withheld", "‡", "†"}
NOT_APPLICABLE_HINTS = {"n/a", "not applicable", "--", "—", "–", "-"}
NOT_COLLECTED_HINTS = {"nc", "not collected", "..", "not available", "(na)", "na"}
UNKNOWN_HINTS = {"?", "unknown", "unk", "null", "none"}
_LESS_THAN = re.compile(r"^<\s*\d+$")
_THOUSANDS = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")
#: Tokens whose category is a guess even when recognised.
AMBIGUOUS = {"na", "-", "--", "x", "d", "s"}


def classify_token(token: str) -> str:
    low = token.lower()
    if low in SUPPRESSED_HINTS or _LESS_THAN.match(token):
        return "suppressed"
    if low in NOT_APPLICABLE_HINTS:
        return "not_applicable"
    if low in NOT_COLLECTED_HINTS:
        return "not_collected"
    return "unknown"


def field_name(header: str, used: set[str]) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", header.lower()).strip("_") or "column"
    if name[0].isdigit():
        name = f"f_{name}"
    name = name[:60]
    base, n = name, 2
    while name in used:
        name = f"{base}_{n}"
        n += 1
    used.add(name)
    return name


def _decode(data: bytes) -> tuple[str, str]:
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace"), "cp1252"


def _detect_layout(text: str, url: str) -> tuple[str, str, int]:
    """Return (format, delimiter, skip_rows)."""
    lines = [ln for ln in text.splitlines()[:60]]
    candidates = [",", "\t", ";", "|"]
    best, best_score = ",", -1
    for delim in candidates:
        counts = [len(r) for r in csv.reader(lines, delimiter=delim) if len(r) > 1]
        if not counts:
            continue
        mode, freq = Counter(counts).most_common(1)[0]
        score = freq * mode
        if score > best_score:
            best, best_score = delim, score
    fmt = "tsv" if best == "\t" else "csv"
    if url.lower().endswith(".tsv"):
        fmt, best = "tsv", "\t"
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=best))[:60]
    widths = [len(r) for r in rows if len(r) > 1]
    modal = Counter(widths).most_common(1)[0][0] if widths else 1
    skip = 0
    for index, row in enumerate(rows):
        if len(row) == modal and all(c.strip() for c in row):
            skip = index
            break
    return fmt, best, skip


def _infer_column(values: list[str]) -> dict[str, Any]:
    present = [v for v in values if v != ""]
    numeric, decimals, thousands, others = 0, False, False, Counter()
    dates = bools = 0
    for v in present:
        plain = v.replace(",", "") if _THOUSANDS.match(v) else v
        if _THOUSANDS.match(v):
            thousands = True
        try:
            parse_value("integer", plain, None, None)
            numeric += 1
            continue
        except ValueError:
            pass
        try:
            parse_value("number", plain, None, None)
            numeric += 1
            decimals = True
            continue
        except ValueError:
            pass
        others[v] += 1
        try:
            parse_value("date", v, None, None)
            dates += 1
        except ValueError:
            pass
        if v.lower() in {"true", "false", "yes", "no"}:
            bools += 1
    info: dict[str, Any] = {"values": len(values), "empty": len(values) - len(present),
                            "tokens": {}, "thousands": False}
    # Codes such as "007" are identifiers: reading them as numbers loses the zeros.
    leading_zero = any(re.match(r"^0\d", v) for v in present)
    if present and numeric and numeric >= 0.5 * len(present) and not leading_zero:
        info["type"] = "number" if decimals else "integer"
        info["thousands"] = thousands
        info["tokens"] = {t: classify_token(t) for t in sorted(others)}
    elif present and dates == len(present):
        info["type"] = "date"
    elif present and bools == len(present):
        info["type"] = "boolean"
    else:
        info["type"] = "string"
    return info


def infer_spec(data: bytes, url: str, dataset_id: str, receipt: dict[str, Any]) -> tuple[str, dict]:
    text, encoding = _decode(data)
    stripped = text.lstrip()
    if stripped.startswith("{"):
        raise SpecError("adopt infers CSV and TSV only; write a jsonl spec by hand")
    fmt, delimiter, skip = _detect_layout(text, url)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for _ in range(skip):
        next(reader, None)
    header = [h.strip() for h in next(reader, [])]
    if not header:
        raise SpecError("could not find a header row")
    rows = [r for r in reader if any(c.strip() for c in r)]
    used: set[str] = set()
    columns = []
    for index, source in enumerate(header):
        values = [(r[index].strip() if index < len(r) else "") for r in rows]
        info = _infer_column(values)
        info.update(name=field_name(source, used), source=source,
                    distinct=len(set(values)))
        columns.append(info)

    tokens: dict[str, set[str]] = {}
    review_tokens = []
    for col in columns:
        for token, category in col["tokens"].items():
            tokens.setdefault(category, set()).add(token)
            if category == "unknown" or token.lower() in AMBIGUOUS:
                review_tokens.append(token)
    if any(c["empty"] for c in columns):
        tokens.setdefault("unknown", set()).add("")
    thousands = any(c["thousands"] for c in columns)
    key = next((c["name"] for c in columns
                if c["type"] in ("string", "integer") and c["empty"] == 0
                and c["distinct"] == len(rows) and not c["tokens"]), None)

    title = Path(urlparse(url).path).stem.replace("_", " ").replace("-", " ") or dataset_id
    L: list[str] = []
    L.append(f"# Draft spec inferred by {TOOL} adopt on {today()}.")
    L.append(f"# Source: {url}")
    L.append(f"# Captured: sha256 {receipt['sha256']} (receipt {receipt['receipt_id']}).")
    L.append("# Review every line marked REVIEW, fill in who publishes this and its rights,")
    L.append("# then delete `draft = true` and run `pallet describe` on this file.")
    L.append("")
    L.append("[pallet]")
    L.append("spec = 1")
    L.append("draft = true")
    L.append("")
    L.append("[dataset]")
    L.append(f"id = {tv(dataset_id)}")
    L.append(f"title = {tv(title)}  # REVIEW")
    L.append('description = "REVIEW: what this table is and what one row means"')
    L.append('publisher = "REVIEW: who publishes this"')
    L.append('cadence = "unknown"  # REVIEW: e.g. annual, monthly, one-off')
    L.append('grain = ""  # REVIEW: one row per ...')
    L.append('geography = ""  # REVIEW')
    L.append('time_basis = ""  # REVIEW')
    L.append("")
    L.append("[rights]")
    L.append('license = "REVIEW: the licence or terms of use"')
    L.append('redistribution = "unknown"  # REVIEW: allowed | restricted | prohibited')
    L.append("prohibited_uses = []  # REVIEW")
    L.append("")
    L.append("[source]")
    L.append(f"format = {tv(fmt)}")
    L.append(f"encoding = {tv(encoding)}")
    if fmt == "csv" and delimiter != ",":
        L.append(f"delimiter = {tv(delimiter)}")
    if skip:
        L.append(f"skip_rows = {skip}  # REVIEW: {skip} preamble row(s) before the header")
    L.append("")
    L.append("[[source.editions]]")
    L.append('label = "adopted"  # REVIEW: e.g. the year or period this edition covers')
    L.append(f"url = {tv(url)}")
    L.append("")
    L.append("[shape]")
    if key:
        L.append(f"key = [{tv(key)}]  # REVIEW: guessed from the first unique column")
    if thousands:
        L.append('thousands = ","')
    L.append("")
    L.append("[shape.missing]")
    for category in ("suppressed", "not_collected", "not_applicable", "unknown"):
        found = sorted(tokens.get(category, set()))
        if found:
            note = "  # REVIEW" if any(t in review_tokens for t in found) else ""
            L.append(f"{category} = {tv(found)}{note}")
    if not tokens:
        L.append("# No missing-value tokens seen.")
    for col in columns:
        L.append("")
        L.append("[[shape.fields]]")
        L.append(f"name = {tv(col['name'])}")
        L.append(f"source = {tv(col['source'])}")
        L.append(f"type = {tv(col['type'])}  # inferred from {col['values']} cell(s)")
        L.append('unit = ""  # REVIEW')
        L.append('description = ""  # REVIEW')
        if col["tokens"]:
            L.append(f"# tokens seen: {', '.join(repr(t) for t in col['tokens'])}")
    draft = "\n".join(L) + "\n"
    parse_contract(draft, origin="draft spec")  # the draft must be a valid contract
    summary = {
        "format": fmt,
        "encoding": encoding,
        "skip_rows": skip,
        "rows": len(rows),
        "key": key,
        "thousands": thousands,
        "missing": {k: sorted(v) for k, v in tokens.items()},
        "review_tokens": sorted(set(review_tokens)),
        "fields": [{"name": c["name"], "source": c["source"], "type": c["type"],
                    "tokens": c["tokens"]} for c in columns],
    }
    return draft, summary


def adopt(store: Store, source: str, dataset_id: str, out: Path | None = None,
          force: bool = False) -> dict[str, Any]:
    store.require()
    store.dataset_dir(dataset_id)  # validates the id
    if store.has_dataset(dataset_id):
        raise PreconditionError(f"{dataset_id} is already described; adopt is for new datasets")
    url = source if urlparse(source).scheme else Path(source).resolve().as_uri()
    out = Path(out) if out else Path(f"{dataset_id}.draft.toml")
    if out.exists() and not force:
        raise PreconditionError(f"{out} exists; pass --force to overwrite it")
    receipt = retrieve(store, dataset_id, "adopt", None, url)
    draft, summary = infer_spec(store.read_raw(receipt["sha256"]), url, dataset_id, receipt)
    out.write_text(draft, encoding="utf-8")
    return {"dataset_id": dataset_id, "draft": str(out), "url": url,
            "sha256": receipt["sha256"], "receipt_id": receipt["receipt_id"],
            "released": False, **summary}
