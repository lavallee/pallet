"""The `pallet` command line. Every command takes --json; exit codes are in errors.py."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pallet import TOOL, __version__
from pallet.adopt import adopt
from pallet.capture import capture
from pallet.errors import EXIT_CODES, PalletError, PreconditionError, SpecError
from pallet.release import (
    build_release,
    export,
    load_pin,
    make_pin,
    materialize,
    rollback,
    verify_dataset,
    verify_pin,
)
from pallet.shape import run_shape
from pallet.spec import load_contract
from pallet.store import Store
from pallet.util import read_json, slug, today


@dataclass
class Out:
    payload: dict[str, Any]
    text: str
    code: int = 0
    raw: bool = False  # print the payload itself as JSON in both modes (pins)


Handler = Callable[[argparse.Namespace, Store], Out]


# -- commands -------------------------------------------------------------------------

def cmd_init(args, store: Store) -> Out:
    created = store.init()
    word = "Created" if created else "Already initialised:"
    return Out({"store": str(store.root), "created": created},
               f"{word} pallet store at {store.root}")


def cmd_describe(args, store: Store) -> Out:
    store.require()
    path = Path(args.spec)
    contract = load_contract(path)
    if contract.is_draft:
        line = contract.line_of("pallet", "draft")
        raise SpecError(
            f"{path.name} line {line}: draft = true. Review the inferred spec, then remove "
            "that line before describing it.", draft=True,
        )
    if args.check:
        return Out({"dataset_id": contract.id, "valid": True, "registered": False},
                   f"{path.name}: valid contract for {contract.id}")
    changed = store.register(contract, base_dir=path.resolve().parent)
    state = store.state(contract.id)
    if changed or state is None:
        store.record_state(contract.id, "described", contract_sha256=contract.sha256)
    status = "registered" if changed else "unchanged"
    text = (f"{contract.id}: contract {status} ({len(contract.editions)} edition(s), "
            f"{len(contract.fields)} field(s), redistribution {contract.rights['redistribution']})")
    return Out({"dataset_id": contract.id, "status": status, "contract_sha256": contract.sha256,
                "state": store.state(contract.id)}, text)


def cmd_capture(args, store: Store) -> Out:
    report = capture(store, args.dataset, offline=args.offline)
    lines = []
    for r in report["results"]:
        name = r["edition"] or r["role"]
        lines.append(f"  {name:<16} {r['status']:<10} {r['sha256'][:12]}  {r['url']}")
    verb = "Replayed from custody" if args.offline else "Captured"
    return Out(report, f"{verb} {args.dataset}:\n" + "\n".join(lines))


def cmd_shape(args, store: Store) -> Out:
    summary = run_shape(store, args.dataset)
    lines = [f"Shaped {args.dataset}: {summary['row_count']} row(s), data sha256 "
             f"{summary['data_sha256'][:12]}"]
    for name, counts in summary["state_counts"].items():
        if any(s != "reported" for s in counts):
            parts = ", ".join(f"{s} {n}" for s, n in sorted(counts.items()))
            lines.append(f"  {name}: {parts}")
    for ed in summary["editions"]:
        if ed["absent_fields"]:
            lines.append(f"  edition {ed['edition']} lacks: {', '.join(ed['absent_fields'])} "
                         "(recorded as not_collected)")
    return Out(summary, "\n".join(lines))


def cmd_release(args, store: Store) -> Out:
    result = build_release(store, args.dataset, note=args.note)
    if result["status"] == "unchanged":
        text = (f"{args.dataset}: nothing changed since release {result['release_id']}; "
                "no new release made")
    else:
        text = (f"Released {args.dataset} {result['release_id']} ({result['row_count']} rows), "
                f"manifest sha256 {result['manifest_sha256'][:12]}; verified and current")
    return Out(result, text)


def _dataset_status(store: Store, dataset_id: str) -> dict[str, Any]:
    contract = store.load_contract(dataset_id)
    receipts = store.receipts(dataset_id)
    pointer = store.current(dataset_id)
    release_inputs: dict[str, str] = {}
    if pointer:
        manifest = read_json(store.release_dir(dataset_id, pointer["release_id"])
                             / "manifest.json")
        release_inputs = {i["edition"]: i["sha256"] for i in manifest["inputs"]}
    editions = []
    for edition in contract.editions:
        label = edition["label"]
        mine = [r for r in receipts if r["role"] == "edition" and r["edition"] == label]
        hashes = []
        for r in mine:
            if not hashes or hashes[-1] != r["sha256"]:
                hashes.append(r["sha256"])
        latest = mine[-1]["sha256"] if mine else None
        editions.append({
            "edition": label,
            "captures": len(mine),
            "versions": len(hashes),
            "latest_sha256": latest,
            "last_captured": mine[-1]["retrieved_at"] if mine else None,
            "drift": len(mine) >= 2 and mine[-1]["sha256"] != mine[-2]["sha256"],
            "ever_changed": len(hashes) > 1,
            "in_current_release": release_inputs.get(label),
            "release_behind": bool(pointer) and latest is not None
            and release_inputs.get(label) != latest,
        })
    index = [r for r in receipts if r["role"] == "index"]
    return {
        "dataset_id": dataset_id,
        "title": contract.dataset["title"],
        "state": store.state(dataset_id),
        "current_release": pointer["release_id"] if pointer else None,
        "releases": len(store.releases(dataset_id)),
        "redistribution": contract.rights["redistribution"],
        "issues": len(store.issues(dataset_id, contract)),
        "editions": editions,
        "index_captures": len(index),
        "index_changed": len(index) >= 2 and index[-1]["sha256"] != index[-2]["sha256"],
        "drift": any(e["drift"] for e in editions),
    }


def cmd_status(args, store: Store) -> Out:
    store.require()
    ids = [args.dataset] if args.dataset else store.dataset_ids()
    datasets = [_dataset_status(store, i) for i in ids]
    lines = [f"Store {store.root}: {len(datasets)} dataset(s)"]
    for d in datasets:
        lines.append(f"\n{d['dataset_id']}  [{d['state']}]  current: {d['current_release'] or '-'}"
                     f"  releases: {d['releases']}  issues: {d['issues']}"
                     f"  redistribution: {d['redistribution']}")
        for e in d["editions"]:
            flags = []
            if e["drift"]:
                flags.append("DRIFT: upstream bytes changed at last capture")
            elif e["ever_changed"]:
                flags.append(f"changed upstream before ({e['versions']} versions)")
            if e["release_behind"]:
                flags.append("newer than current release")
            if not e["captures"]:
                flags.append("never captured")
            sha = (e["latest_sha256"] or "-")[:12]
            lines.append(f"  {e['edition']:<16} captures {e['captures']:<3} {sha}  "
                         + "; ".join(flags))
        if d["index_changed"]:
            lines.append("  index page changed at last capture")
    return Out({"store": str(store.root), "datasets": datasets}, "\n".join(lines))


def cmd_show(args, store: Store) -> Out:
    contract = store.load_contract(args.dataset)
    pointer = store.current(args.dataset)
    releases = []
    for release_id in store.releases(args.dataset):
        manifest = read_json(store.release_dir(args.dataset, release_id) / "manifest.json")
        releases.append({"release_id": release_id, "created_at": manifest["created_at"],
                         "rows": manifest["row_count"], "note": manifest.get("note"),
                         "current": bool(pointer and pointer["release_id"] == release_id)})
    issues = store.issues(args.dataset, contract)
    payload = {
        "dataset_id": contract.id,
        "contract_sha256": contract.sha256,
        "state": store.state(args.dataset),
        "dataset": contract.dataset,
        "rights": contract.rights,
        "editions": contract.editions,
        "fields": contract.shape["fields"],
        "key": contract.shape.get("key", []),
        "releases": releases,
        "current_release": pointer["release_id"] if pointer else None,
        "issues": issues,
        "lifecycle": store.lifecycle(args.dataset),
    }
    ds, rights = contract.dataset, contract.rights
    lines = [f"{ds['title']} ({contract.id}) [{payload['state']}]",
             f"  {ds['description']}",
             f"  publisher: {ds['publisher']}; cadence: {ds['cadence']}",
             f"  rights: {rights['license']}; redistribution {rights['redistribution']}"]
    for use in rights.get("prohibited_uses", []):
        lines.append(f"    do not: {use}")
    lines.append("  editions: " + ", ".join(e["label"] for e in contract.editions))
    lines.append("  fields:")
    for f in contract.fields:
        unit = f" [{f.unit}]" if f.unit else ""
        lines.append(f"    {f.name}: {f.type}{unit} {f.description}".rstrip())
    lines.append("  releases:" if releases else "  releases: none")
    for r in releases:
        mark = " (current)" if r["current"] else ""
        note = f" - {r['note']}" if r["note"] else ""
        lines.append(f"    {r['release_id']}{mark} {r['rows']} rows{note}")
    lines.append("  issues:" if issues else "  issues: none recorded")
    for i in issues:
        lines.append(f"    [{i.get('severity', 'caution')}] {i['id']}: {i['title']}")
        lines.append(f"      {i['effect']}")
        if i.get("instead"):
            lines.append(f"      instead: {i['instead']}")
    return Out(payload, "\n".join(lines))


def cmd_verify(args, store: Store) -> Out:
    if args.pin:
        pin = load_pin(Path(args.pin))
        if args.dir:
            report = verify_pin(pin, directory=Path(args.dir))
        else:
            store.require()
            report = verify_pin(pin, store=store)
        where = args.dir or "the store"
        return Out(report, f"OK: {where} matches pin {pin['dataset_id']} {pin['release_id']} "
                           f"({report['checked']} files checked)")
    if not args.dataset:
        raise PreconditionError("verify needs a dataset or --pin FILE")
    report = verify_dataset(store, args.dataset)
    return Out(report, f"OK: {args.dataset}: {len(report['releases'])} release(s) and the "
                       "current pointer verified")


def cmd_pin(args, store: Store) -> Out:
    pin = make_pin(store, args.dataset, args.release)
    text = json.dumps(pin, indent=2, sort_keys=True)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    return Out(pin, text, raw=True)


def cmd_materialize(args, store: Store) -> Out:
    store.require()
    pin = load_pin(Path(args.pin))
    report = materialize(store, pin, Path(args.to), private=args.private)
    mark = " (marked not for redistribution)" if report["not_for_redistribution"] else ""
    return Out(report, f"Materialized {pin['dataset_id']} {pin['release_id']} to {args.to} "
                       f"and verified it against the pin{mark}")


def cmd_rollback(args, store: Store) -> Out:
    result = rollback(store, args.dataset, to=args.to)
    return Out(result, f"{args.dataset}: current release moved from "
                       f"{result['rolled_back_from']} back to {result['release_id']}")


def cmd_deprecate(args, store: Store) -> Out:
    store.require_dataset(args.dataset)
    store.record_state(args.dataset, "deprecated", reason=args.reason)
    return Out({"dataset_id": args.dataset, "state": "deprecated", "reason": args.reason},
               f"{args.dataset} deprecated: {args.reason}. Existing releases and pins still "
               "verify; no new releases will be made.")


def cmd_issue_add(args, store: Store) -> Out:
    issue: dict[str, Any] = {
        "id": args.id or slug(args.title),
        "title": args.title,
        "severity": args.severity,
        "effect": args.effect,
    }
    optional = {
        "misuse": args.misuse, "instead": args.instead, "fields": args.field,
        "editions": args.edition, "publisher_quote": args.quote,
        "quote_source": args.quote_source, "quote_date": args.quote_date,
    }
    issue.update({k: v for k, v in optional.items() if v})
    issue["recorded"] = args.recorded or today()
    if issue.get("publisher_quote") and not issue.get("quote_date"):
        raise SpecError("a publisher quote needs --quote-date (when the publisher said it)")
    added = store.add_issue(args.dataset, issue)
    return Out({"dataset_id": args.dataset, "issue": added},
               f"{args.dataset}: recorded issue {added['id']}; it ships in the next release")


def cmd_issue_list(args, store: Store) -> Out:
    issues = store.issues(args.dataset)
    lines = [f"[{i.get('severity', 'caution')}] {i['id']}: {i['title']}" for i in issues]
    return Out({"dataset_id": args.dataset, "issues": issues},
               "\n".join(lines) or f"{args.dataset}: no issues recorded")


def cmd_export(args, store: Store) -> Out:
    result = export(store, args.dataset, Path(args.to), private=args.private,
                    release_id=args.release)
    mark = " (marked not for redistribution)" if result["not_for_redistribution"] else ""
    return Out(result, f"Exported {args.dataset} {result['release_id']} as a data package to "
                       f"{args.to}{mark}")


def cmd_adopt(args, store: Store) -> Out:
    result = adopt(store, args.source, args.id, out=Path(args.out) if args.out else None,
                   force=args.force)
    lines = [f"Captured {result['url']} (sha256 {result['sha256'][:12]}) and wrote a draft spec "
             f"to {result['draft']}.",
             f"  {result['rows']} row(s), format {result['format']}, "
             f"key guess: {result['key'] or 'none'}"]
    for f in result["fields"]:
        tokens = f" tokens {sorted(f['tokens'])}" if f["tokens"] else ""
        lines.append(f"  {f['name']}: {f['type']}{tokens}")
    if result["review_tokens"]:
        lines.append(f"  REVIEW these tokens: {result['review_tokens']}")
    lines.append("Nothing was released. Review the draft, remove `draft = true`, then run "
                 "`pallet describe` on it.")
    return Out(result, "\n".join(lines))


# -- parser -------------------------------------------------------------------------------

def _common() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print a JSON object on stdout")
    common.add_argument("--store", default=argparse.SUPPRESS,
                        help="store directory (default $PALLET_STORE or ./pallet-store)")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _common()
    epilog = "exit codes:\n" + "\n".join(f"  {k}  {v}" for k, v in EXIT_CODES.items())
    parser = argparse.ArgumentParser(
        prog="pallet", parents=[common], epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Acquire datasets with custody: capture, shape, release, pin, verify.",
    )
    parser.add_argument("--version", action="version", version=TOOL)
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, parents=[common], help=help_text, description=help_text)
        p.set_defaults(func=handler)
        return p

    add("init", cmd_init, "create a store")
    p = add("describe", cmd_describe, "register or validate a dataset contract (spec TOML)")
    p.add_argument("spec")
    p.add_argument("--check", action="store_true", help="validate only; do not register")
    p = add("capture", cmd_capture, "capture every edition and the index page into custody")
    p.add_argument("dataset")
    p.add_argument("--offline", action="store_true",
                   help="no network: re-verify the latest capture of every edition")
    p = add("shape", cmd_shape, "shape captured bytes into a typed table (no network)")
    p.add_argument("dataset")
    p = add("release", cmd_release, "build, verify, promote atomically, verify again")
    p.add_argument("dataset")
    p.add_argument("--note")
    p = add("status", cmd_status, "lifecycle, releases, and upstream drift for each dataset")
    p.add_argument("dataset", nargs="?")
    p = add("show", cmd_show, "contract, releases, and issues of a dataset")
    p.add_argument("dataset")
    p = add("verify", cmd_verify, "verify a dataset's releases, or a copy against a pin")
    p.add_argument("dataset", nargs="?")
    p.add_argument("--pin", help="pin file to verify against")
    p.add_argument("--dir", help="with --pin: verify this directory instead of the store")
    p = add("pin", cmd_pin, "print a pin for the current (or a given) release")
    p.add_argument("dataset")
    p.add_argument("--release")
    p.add_argument("--out", help="also write the pin to this file")
    p = add("materialize", cmd_materialize, "copy a pinned release out for offline use")
    p.add_argument("--pin", required=True)
    p.add_argument("--to", required=True)
    p.add_argument("--private", action="store_true",
                   help="allow a non-redistributable dataset; marks the copy")
    p = add("rollback", cmd_rollback, "move the current pointer back to the previous release")
    p.add_argument("dataset")
    p.add_argument("--to", help="an earlier promoted release id")
    p = add("deprecate", cmd_deprecate, "mark a dataset deprecated; no further releases")
    p.add_argument("dataset")
    p.add_argument("--reason", required=True)

    p = sub.add_parser("issue", parents=[common], help="record and list known issues")
    isub = p.add_subparsers(dest="issue_command", required=True, metavar="ACTION")
    a = isub.add_parser("add", parents=[common], help="record a known issue")
    a.set_defaults(func=cmd_issue_add)
    a.add_argument("dataset")
    a.add_argument("--title", required=True)
    a.add_argument("--effect", required=True, help="what is wrong and what it does to numbers")
    a.add_argument("--id")
    a.add_argument("--severity", choices=["info", "caution", "warning"], default="caution")
    a.add_argument("--misuse", help="the mistake this invites")
    a.add_argument("--instead", help="what to do instead")
    a.add_argument("--field", action="append", help="affected field (repeatable)")
    a.add_argument("--edition", action="append", help="affected edition (repeatable)")
    a.add_argument("--quote", help="the publisher's own words, verbatim")
    a.add_argument("--quote-source", help="where the publisher said it")
    a.add_argument("--quote-date", help="when the publisher said it")
    a.add_argument("--recorded", help="date recorded (default today)")
    lst = isub.add_parser("list", parents=[common], help="list known issues")
    lst.set_defaults(func=cmd_issue_list)
    lst.add_argument("dataset")

    p = add("export", cmd_export, "export the current release as a Frictionless Data Package")
    p.add_argument("dataset")
    p.add_argument("--to", required=True)
    p.add_argument("--release")
    p.add_argument("--private", action="store_true",
                   help="allow a non-redistributable dataset; marks the output")
    p = add("adopt", cmd_adopt, "capture one CSV/TSV and infer a draft spec to review")
    p.add_argument("source", help="file path or URL")
    p.add_argument("--id", required=True, help="dataset id for the draft")
    p.add_argument("--out", help="where to write the draft (default ./<id>.draft.toml)")
    p.add_argument("--force", action="store_true", help="overwrite an existing draft")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    as_json = getattr(args, "json", False)
    store = Store.resolve(getattr(args, "store", None))
    try:
        out = args.func(args, store)
    except PalletError as exc:
        if as_json:
            print(json.dumps({"ok": False, "error": {"kind": exc.kind, "code": exc.exit_code,
                                                     "message": exc.message,
                                                     "detail": exc.detail}},
                             indent=2, sort_keys=True, default=str))
        else:
            print(f"pallet: {exc.kind}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    if out.raw:
        print(json.dumps(out.payload, indent=2, sort_keys=True))
    elif as_json:
        print(json.dumps({"ok": True, **out.payload}, indent=2, sort_keys=True, default=str))
    else:
        print(out.text)
    return out.code


__all__ = ["main", "build_parser", "__version__"]
