"""Immutable releases, the current pointer, pins, materialize, export."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from pallet import TOOL, validation
from pallet.errors import (
    NotFoundError,
    PreconditionError,
    RefusedError,
    SpecError,
    VerifyError,
)
from pallet.package import (
    NOT_FOR_REDISTRIBUTION,
    build_datapackage,
    check_datapackage,
    render_readme,
)
from pallet.shape import gather_inputs, shape_inputs
from pallet.spec import Contract, parse_contract
from pallet.store import Store
from pallet.util import (
    atomic_write,
    canonical_json,
    iso,
    now_iso,
    read_json,
    sha256_bytes,
    sha256_file,
    utc_now,
)

MEDIA_TYPES = {
    "data.csv": "text/csv",
    "datapackage.json": "application/json",
    "issues.json": "application/json",
    "README.md": "text/markdown",
    "contract.toml": "application/toml",
}
MARKER_FILE = "NOT-FOR-REDISTRIBUTION.txt"


# -- verification ------------------------------------------------------------------

def check_release_dir(folder: Path, strict: bool = True) -> tuple[dict | None, str | None, list]:
    """Check a release directory against its manifest. Returns (manifest, sha, problems)."""
    path = folder / "manifest.json"
    if not path.is_file():
        return None, None, [f"{folder.name}: manifest.json is missing"]
    raw = path.read_bytes()
    manifest_sha = sha256_bytes(raw)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, manifest_sha, [f"manifest.json is not JSON: {exc}"]
    problems = [f"manifest: {e}" for e in validation.errors(manifest, "manifest")]
    if problems:
        return manifest, manifest_sha, problems
    listed = {"manifest.json"}
    for artifact in manifest["artifacts"]:
        name = artifact["path"]
        listed.add(name)
        file = folder / name
        if not file.is_file():
            problems.append(f"{name} is missing")
        elif file.stat().st_size != artifact["bytes"]:
            problems.append(f"{name} is {file.stat().st_size} bytes, manifest says "
                            f"{artifact['bytes']}")
        elif sha256_file(file) != artifact["sha256"]:
            problems.append(f"{name} does not match its SHA-256 in the manifest")
    if strict:
        for extra in sorted(p.name for p in folder.iterdir() if p.name not in listed):
            problems.append(f"unexpected file {extra}")
    return manifest, manifest_sha, problems


def verify_current(store: Store, dataset_id: str) -> dict[str, Any]:
    pointer = store.current(dataset_id)
    if pointer is None:
        raise NotFoundError(f"{dataset_id} has no current release")
    folder = store.release_dir(dataset_id, pointer["release_id"])
    manifest, sha, problems = check_release_dir(folder)
    if sha is not None and sha != pointer["manifest_sha256"]:
        problems.append("current.json names a manifest hash the release does not have")
    report = {"release_id": pointer["release_id"], "ok": not problems, "problems": problems}
    if problems:
        raise VerifyError(f"current release of {dataset_id} failed verification: "
                          + "; ".join(problems), **report)
    return report


def verify_dataset(store: Store, dataset_id: str) -> dict[str, Any]:
    """Verify every release, the pointer, and the raw custody each release was shaped from."""
    store.require_dataset(dataset_id)
    releases = []
    ok = True
    for release_id in store.releases(dataset_id):
        manifest, sha, problems = check_release_dir(store.release_dir(dataset_id, release_id))
        for item in (manifest or {}).get("inputs", []) if not problems else []:
            try:
                store.read_raw(item["sha256"])
            except VerifyError as exc:
                problems.append(f"input {item['edition']}: {exc.message}")
        ok = ok and not problems
        releases.append({"release_id": release_id, "manifest_sha256": sha, "ok": not problems,
                         "problems": problems})
    pointer = store.current(dataset_id)
    current: dict[str, Any] | None = None
    if pointer is not None:
        try:
            current = verify_current(store, dataset_id)
        except VerifyError as exc:
            current = {"release_id": pointer["release_id"], "ok": False,
                       "problems": exc.detail.get("problems", [exc.message])}
            ok = False
    report = {"dataset_id": dataset_id, "ok": ok, "current": current, "releases": releases}
    if not ok:
        bad = [r["release_id"] for r in releases if not r["ok"]]
        raise VerifyError(f"{dataset_id}: verification failed for {', '.join(bad) or 'current'}",
                          **report)
    return report


# -- pointer -------------------------------------------------------------------------

def _write_pointer(store: Store, dataset_id: str, release_id: str, manifest_sha: str,
                   lineage: list[str], action: str) -> dict[str, Any]:
    previous = store.current(dataset_id) or {}
    log = list(previous.get("log", []))
    log.append({"action": action, "release_id": release_id, "at": now_iso()})
    doc = {
        "pallet_pointer": 1,
        "dataset_id": dataset_id,
        "release_id": release_id,
        "manifest_sha256": manifest_sha,
        "updated_at": now_iso(),
        "lineage": lineage,
        "log": log,
    }
    atomic_write(store.current_path(dataset_id), canonical_json(doc))
    return doc


def _promote(store: Store, dataset_id: str, staging: Path, release_id: str,
             manifest_sha: str) -> None:
    """Move a verified candidate into place, verify it again, then move the pointer."""
    final = store.release_dir(dataset_id, release_id)
    final.parent.mkdir(parents=True, exist_ok=True)
    os.rename(staging, final)
    try:
        for item in final.iterdir():
            os.chmod(item, 0o444)
        _, sha, problems = check_release_dir(final)
        if problems or sha != manifest_sha:
            raise VerifyError("release changed while being promoted: " + "; ".join(problems))
        lineage = list((store.current(dataset_id) or {}).get("lineage", [])) + [release_id]
        _write_pointer(store, dataset_id, release_id, manifest_sha, lineage, "promote")
    except BaseException:
        pointer = store.current(dataset_id)
        if not pointer or pointer["release_id"] != release_id:
            shutil.rmtree(final, ignore_errors=True)
        raise


# -- build -----------------------------------------------------------------------------

def build_release(store: Store, dataset_id: str, note: str | None = None) -> dict[str, Any]:
    contract = store.load_contract(dataset_id)
    with store.lock(dataset_id):
        store.require_state(dataset_id, ("shaped", "released"), "release")
        folder = store.dataset_dir(dataset_id)
        shaped_path = folder / "shaped" / "shape.json"
        if not shaped_path.exists():
            raise PreconditionError(f"{dataset_id} has not been shaped (run `pallet shape`)")
        shaped = read_json(shaped_path)
        inputs = gather_inputs(store, contract)
        if shaped["contract_sha256"] != contract.sha256 or shaped["inputs"] != inputs:
            raise PreconditionError(
                f"the contract or captures of {dataset_id} changed since it was shaped "
                "(run `pallet shape`)"
            )
        result = shape_inputs(store, contract, inputs)
        data = result.to_csv()
        data_sha = sha256_bytes(data)
        if data_sha != shaped["data_sha256"]:
            raise VerifyError("re-shaping the captured bytes did not reproduce the shaped table")
        issues = store.issues(dataset_id, contract)
        content_sha = sha256_bytes(canonical_json({
            "contract_sha256": contract.sha256,
            "inputs": [[i["edition"], i["sha256"]] for i in inputs],
            "data_sha256": data_sha,
            "issues_sha256": sha256_bytes(canonical_json(issues)),
        }))
        pointer = store.current(dataset_id)
        if pointer:
            current_manifest = read_json(
                store.release_dir(dataset_id, pointer["release_id"]) / "manifest.json")
            if current_manifest["content_sha256"] == content_sha:
                return {"status": "unchanged", "dataset_id": dataset_id,
                        "release_id": pointer["release_id"],
                        "manifest_sha256": pointer["manifest_sha256"]}

        created = utc_now()
        release_id = f"{created:%Y%m%dT%H%M%SZ}-{content_sha[:8]}"
        if store.release_dir(dataset_id, release_id).exists():
            raise PreconditionError(f"release {release_id} already exists; try again")
        staging_root = folder / ".staging"
        if staging_root.exists():  # left behind by a run that was killed
            shutil.rmtree(staging_root)
        staging = staging_root / release_id
        staging.mkdir(parents=True)
        try:
            package = build_datapackage(
                contract, release_id=release_id, created_at=iso(created), data_sha256=data_sha,
                data_bytes=len(data), row_count=len(result.rows),
                state_columns=result.state_columns, state_counts=result.state_counts,
                inputs=inputs, issues=issues,
            )
            files = {
                "data.csv": data,
                "datapackage.json": canonical_json(package),
                "README.md": render_readme(contract, package).encode("utf-8"),
                "issues.json": canonical_json({"dataset_id": dataset_id,
                                               "release_id": release_id, "issues": issues}),
                "contract.toml": contract.text.encode("utf-8"),
            }
            artifacts = []
            for name, payload in files.items():
                (staging / name).write_bytes(payload)
                artifacts.append({"path": name, "sha256": sha256_bytes(payload),
                                  "bytes": len(payload), "media_type": MEDIA_TYPES[name]})
            manifest = {
                "pallet_manifest": 1,
                "dataset_id": dataset_id,
                "release_id": release_id,
                "created_at": iso(created),
                "tool": TOOL,
                "note": note,
                "contract_sha256": contract.sha256,
                "content_sha256": content_sha,
                "previous_release": pointer["release_id"] if pointer else None,
                "redistribution": contract.rights["redistribution"],
                "row_count": len(result.rows),
                "inputs": inputs,
                "artifacts": artifacts,
            }
            validation.validate(manifest, "manifest", what="release manifest")
            manifest_bytes = canonical_json(manifest)
            (staging / "manifest.json").write_bytes(manifest_bytes)
            manifest_sha = sha256_bytes(manifest_bytes)
            _, sha, problems = check_release_dir(staging)
            if problems or sha != manifest_sha:
                raise VerifyError("candidate release failed verification: " + "; ".join(problems))
            _promote(store, dataset_id, staging, release_id, manifest_sha)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        after = verify_current(store, dataset_id)
        store.record_state(dataset_id, "released", release_id=release_id)
    return {
        "status": "released",
        "dataset_id": dataset_id,
        "release_id": release_id,
        "manifest_sha256": manifest_sha,
        "previous_release": manifest["previous_release"],
        "row_count": len(result.rows),
        "verified": after["ok"],
    }


def rollback(store: Store, dataset_id: str, to: str | None = None) -> dict[str, Any]:
    store.require_dataset(dataset_id)
    with store.lock(dataset_id):
        pointer = store.current(dataset_id)
        if pointer is None:
            raise PreconditionError(f"{dataset_id} has no current release to roll back")
        lineage = list(pointer.get("lineage", [pointer["release_id"]]))
        if to is not None:
            if to not in lineage[:-1]:
                raise PreconditionError(f"{to} is not an earlier promoted release of {dataset_id}")
            new_lineage = lineage[: lineage.index(to) + 1]
        else:
            if len(lineage) < 2:
                raise PreconditionError(f"{dataset_id} has no earlier release to roll back to")
            new_lineage = lineage[:-1]
        target = new_lineage[-1]
        _, sha, problems = check_release_dir(store.release_dir(dataset_id, target))
        if problems:
            raise VerifyError(f"rollback target {target} failed verification: "
                              + "; ".join(problems))
        _write_pointer(store, dataset_id, target, sha, new_lineage, "rollback")
        verify_current(store, dataset_id)
        store.record_state(dataset_id, "released", release_id=target,
                           rolled_back_from=pointer["release_id"])
    return {"dataset_id": dataset_id, "release_id": target, "manifest_sha256": sha,
            "rolled_back_from": pointer["release_id"]}


# -- pins -------------------------------------------------------------------------------

def make_pin(store: Store, dataset_id: str, release_id: str | None = None) -> dict[str, Any]:
    store.require_dataset(dataset_id)
    if release_id is None:
        pointer = store.current(dataset_id)
        if pointer is None:
            raise NotFoundError(f"{dataset_id} has no current release; pass --release")
        release_id = pointer["release_id"]
    folder = store.release_dir(dataset_id, release_id)
    if not folder.is_dir():
        raise NotFoundError(f"{dataset_id} has no release {release_id}")
    manifest, sha, problems = check_release_dir(folder)
    if problems:
        raise VerifyError(f"release {release_id} failed verification: " + "; ".join(problems))
    package = read_json(folder / "datapackage.json")
    issues = read_json(folder / "issues.json")["issues"]
    pin = {
        "pallet_pin": 1,
        "dataset_id": dataset_id,
        "title": package.get("title", ""),
        "release_id": release_id,
        "manifest_sha256": sha,
        "artifacts": [{"path": a["path"], "sha256": a["sha256"], "bytes": a["bytes"]}
                      for a in manifest["artifacts"]],
        "redistribution": manifest["redistribution"],
        "license": package["licenses"][0].get("name", ""),
        "issue_ids": [i["id"] for i in issues],
        "pinned_at": now_iso(),
        "tool": TOOL,
    }
    validation.validate(pin, "pin", what="pin")
    return pin


def load_pin(path: Path) -> dict[str, Any]:
    try:
        pin = json.loads(Path(path).read_text("utf-8"))
    except FileNotFoundError as exc:
        raise NotFoundError(f"pin file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"pin file is not JSON: {exc}") from exc
    validation.validate(pin, "pin", what=f"pin {Path(path).name}")
    return pin


def verify_pin(pin: dict[str, Any], store: Store | None = None,
               directory: Path | None = None) -> dict[str, Any]:
    """Prove a copy matches a pin. Reads only local files."""
    if directory is not None:
        folder, location = Path(directory), "directory"
    else:
        assert store is not None
        folder, location = store.release_dir(pin["dataset_id"], pin["release_id"]), "store"
    if not folder.is_dir():
        raise NotFoundError(f"no copy of {pin['dataset_id']} {pin['release_id']} at {folder}")
    problems: list[str] = []
    manifest_path = folder / "manifest.json"
    if not manifest_path.is_file():
        problems.append("manifest.json is missing")
    else:
        if sha256_file(manifest_path) != pin["manifest_sha256"]:
            problems.append("manifest.json does not match the pinned manifest hash")
        try:
            manifest = read_json(manifest_path)
            if (manifest.get("dataset_id"), manifest.get("release_id")) != (
                pin["dataset_id"], pin["release_id"]
            ):
                problems.append("manifest names a different dataset or release")
            listed = sorted((a["path"], a["sha256"], a["bytes"])
                            for a in manifest.get("artifacts", []))
            pinned = sorted((a["path"], a["sha256"], a["bytes"]) for a in pin["artifacts"])
            if listed != pinned:
                problems.append("manifest artifacts differ from the pinned artifacts")
        except (json.JSONDecodeError, KeyError, TypeError):
            problems.append("manifest.json is not a readable manifest")
    for artifact in pin["artifacts"]:
        file = folder / artifact["path"]
        if not file.is_file():
            problems.append(f"{artifact['path']} is missing")
        elif file.stat().st_size != artifact["bytes"]:
            problems.append(f"{artifact['path']} has the wrong size")
        elif sha256_file(file) != artifact["sha256"]:
            problems.append(f"{artifact['path']} does not match its pinned SHA-256")
    report = {"ok": not problems, "dataset_id": pin["dataset_id"],
              "release_id": pin["release_id"], "location": location,
              "checked": len(pin["artifacts"]) + 1, "problems": problems}
    if problems:
        raise VerifyError(f"{pin['dataset_id']} {pin['release_id']} does not match its pin: "
                          + "; ".join(problems), **report)
    return report


# -- rights, materialize, export ------------------------------------------------------------

def check_rights(contract: Contract, release_id: str, action: str, private: bool) -> None:
    if contract.redistributable or private:
        return
    value = contract.rights["redistribution"]
    line = contract.line_of("rights", "redistribution")
    where = f"contract.toml line {line}" if line else "contract.toml [rights]"
    raise RefusedError(
        f"refusing to {action} {contract.id} release {release_id}: {where}: "
        f'redistribution = "{value}". Pass --private for a personal copy marked '
        "not for redistribution.",
        contract_line=line, redistribution=value, dataset_id=contract.id,
    )


def _prepare_target(to: Path) -> Path:
    to = Path(to)
    if to.exists() and (not to.is_dir() or any(to.iterdir())):
        raise PreconditionError(f"{to} already exists and is not an empty directory")
    to.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=to.parent, prefix=f".{to.name}."))


def _finish_target(tmp: Path, to: Path) -> None:
    if to.exists():
        to.rmdir()
    os.rename(tmp, to)


def _marker_text(dataset_id: str, release_id: str) -> bytes:
    return f"{NOT_FOR_REDISTRIBUTION}\n\nDataset: {dataset_id}\nRelease: {release_id}\n".encode()


def materialize(store: Store, pin: dict[str, Any], to: Path, private: bool = False):
    verify_pin(pin, store=store)
    source = store.release_dir(pin["dataset_id"], pin["release_id"])
    contract = parse_contract((source / "contract.toml").read_text("utf-8"), "contract.toml")
    check_rights(contract, pin["release_id"], "materialize", private)
    tmp = _prepare_target(to)
    try:
        for name in ["manifest.json"] + [a["path"] for a in pin["artifacts"]]:
            shutil.copyfile(source / name, tmp / name)
        (tmp / "pin.json").write_bytes(canonical_json(pin))
        if not contract.redistributable:
            (tmp / MARKER_FILE).write_bytes(_marker_text(contract.id, pin["release_id"]))
        _finish_target(tmp, Path(to))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    report = verify_pin(pin, directory=Path(to))
    report["to"] = str(to)
    report["not_for_redistribution"] = not contract.redistributable
    return report


def export(store: Store, dataset_id: str, to: Path, private: bool = False,
           release_id: str | None = None) -> dict[str, Any]:
    pin = make_pin(store, dataset_id, release_id)
    source = store.release_dir(dataset_id, pin["release_id"])
    contract = parse_contract((source / "contract.toml").read_text("utf-8"), "contract.toml")
    check_rights(contract, pin["release_id"], "export", private)
    marked = not contract.redistributable
    tmp = _prepare_target(to)
    try:
        for name in ("data.csv", "issues.json", "contract.toml", "manifest.json"):
            shutil.copyfile(source / name, tmp / name)
        if marked:
            package = read_json(source / "datapackage.json")
            package["pallet"]["not_for_redistribution"] = True
            package["description"] = f"{NOT_FOR_REDISTRIBUTION} {package['description']}"
            (tmp / "datapackage.json").write_bytes(canonical_json(package))
            readme = render_readme(contract, package, private=True)
            (tmp / "README.md").write_text(readme, encoding="utf-8")
            (tmp / MARKER_FILE).write_bytes(_marker_text(dataset_id, pin["release_id"]))
        else:
            for name in ("datapackage.json", "README.md"):
                shutil.copyfile(source / name, tmp / name)
        (tmp / "pin.json").write_bytes(canonical_json(pin))
        problems = check_datapackage(tmp)
        if problems:
            raise VerifyError("export is not a valid data package: " + "; ".join(problems),
                              problems=problems)
        _finish_target(tmp, Path(to))
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"dataset_id": dataset_id, "release_id": pin["release_id"], "to": str(to),
            "manifest_sha256": pin["manifest_sha256"], "not_for_redistribution": marked,
            "files": sorted(p.name for p in Path(to).iterdir())}
