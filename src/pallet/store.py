"""The on-disk store: raw custody, receipts, contracts, issues, releases, pointers.

Layout (all paths relative to the store root)::

    pallet-store.json                       marker written by `pallet init`
    raw/<sha256>/<filename>                 captured bytes, write-once, read-only
    receipts/<dataset>.jsonl                one line per retrieval, append-only
    datasets/<dataset>/contract.toml        the registered spec, byte-for-byte
    datasets/<dataset>/registration.json    where the spec came from, its hash
    datasets/<dataset>/issues.toml          issues added after registration
    datasets/<dataset>/lifecycle.jsonl      explicit lifecycle events, append-only
    datasets/<dataset>/shaped/              latest shaping output (a preview)
    datasets/<dataset>/releases/<id>/       immutable releases
    datasets/<dataset>/current.json         the pointer to the current release
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pallet import TOOL, tomlw, validation
from pallet.errors import NotFoundError, PreconditionError, SpecError, VerifyError
from pallet.spec import Contract, parse_contract, parse_issues_file, validate_issue
from pallet.util import (
    ID_PATTERN,
    append_line,
    atomic_write,
    canonical_json,
    compact_json,
    fsync_dir,
    now_iso,
    read_json,
    read_jsonl,
    sha256_bytes,
    sha256_file,
)

try:  # POSIX advisory locks; a no-op elsewhere.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

DEFAULT_STORE = "pallet-store"
STATES = ("described", "captured", "shaped", "released", "deprecated")
ISSUES_HEADER = (
    "# Issues recorded for this dataset after its contract was registered.\n"
    "# Managed by `pallet issue add`; issues in the contract itself are kept there.\n"
)


def safe_filename(url: str) -> str:
    name = unquote(Path(urlparse(url).path).name)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return (name or "object")[:100]


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @classmethod
    def resolve(cls, arg: str | None) -> Store:
        return cls(Path(arg or os.environ.get("PALLET_STORE") or DEFAULT_STORE))

    # -- layout -----------------------------------------------------------
    @property
    def marker(self) -> Path:
        return self.root / "pallet-store.json"

    @property
    def raw_root(self) -> Path:
        return self.root / "raw"

    @property
    def receipts_root(self) -> Path:
        return self.root / "receipts"

    @property
    def datasets_root(self) -> Path:
        return self.root / "datasets"

    def dataset_dir(self, dataset_id: str) -> Path:
        if not ID_PATTERN.match(dataset_id):
            raise SpecError(f"invalid dataset id {dataset_id!r}")
        return self.datasets_root / dataset_id

    def release_dir(self, dataset_id: str, release_id: str) -> Path:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", release_id):
            raise SpecError(f"invalid release id {release_id!r}")
        return self.dataset_dir(dataset_id) / "releases" / release_id

    def init(self) -> bool:
        created = not self.marker.exists()
        for sub in (self.raw_root, self.receipts_root, self.datasets_root):
            sub.mkdir(parents=True, exist_ok=True)
        if created:
            atomic_write(self.marker, canonical_json({"pallet_store": 1, "created_at": now_iso(),
                                                      "tool": TOOL}))
        return created

    def require(self) -> None:
        if not self.marker.exists():
            raise PreconditionError(
                f"no pallet store at {self.root} (run `pallet init`, or pass --store)"
            )

    # -- raw custody --------------------------------------------------------
    def raw_file(self, sha: str) -> Path | None:
        folder = self.raw_root / sha
        if not folder.is_dir():
            return None
        files = [p for p in folder.iterdir() if p.is_file()]
        return files[0] if len(files) == 1 else None

    def put_raw(self, data: bytes, filename: str) -> tuple[str, str, bool]:
        """Store bytes under their hash. Returns (sha256, relative path, newly stored)."""
        sha = sha256_bytes(data)
        existing = self.raw_file(sha)
        if existing is not None:
            if sha256_file(existing) != sha:
                raise VerifyError(f"raw object {sha} does not match its hash", sha256=sha)
            return sha, existing.relative_to(self.root).as_posix(), False
        self.raw_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=self.raw_root, prefix=f".{sha[:12]}."))
        target = staging / filename
        with open(target, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o444)
        try:
            os.rename(staging, self.raw_root / sha)  # fails if it already exists
        except OSError:
            shutil.rmtree(staging, ignore_errors=True)
            existing = self.raw_file(sha)
            if existing is None:
                raise
            return sha, existing.relative_to(self.root).as_posix(), False
        fsync_dir(self.raw_root)
        return sha, f"raw/{sha}/{filename}", True

    def read_raw(self, sha: str) -> bytes:
        path = self.raw_file(sha)
        if path is None:
            raise VerifyError(f"raw object {sha} is missing from custody", sha256=sha)
        data = path.read_bytes()
        if sha256_bytes(data) != sha:
            raise VerifyError(f"raw object {sha} does not match its hash", sha256=sha)
        return data

    # -- receipts -----------------------------------------------------------
    def receipts_path(self, dataset_id: str) -> Path:
        self.dataset_dir(dataset_id)  # validates the id
        return self.receipts_root / f"{dataset_id}.jsonl"

    def append_receipt(self, receipt: dict[str, Any]) -> None:
        validation.validate(receipt, "receipt", what="capture receipt")
        append_line(self.receipts_path(receipt["dataset_id"]), compact_json(receipt))

    def receipts(self, dataset_id: str) -> list[dict[str, Any]]:
        return read_jsonl(self.receipts_path(dataset_id))

    def latest_receipts(self, dataset_id: str, role: str = "edition") -> dict[str, dict]:
        latest: dict[str, dict] = {}
        for receipt in self.receipts(dataset_id):
            if receipt["role"] == role:
                latest[receipt["edition"] or ""] = receipt
        return latest

    # -- datasets -------------------------------------------------------------
    def dataset_ids(self) -> list[str]:
        if not self.datasets_root.is_dir():
            return []
        return sorted(p.name for p in self.datasets_root.iterdir()
                      if (p / "contract.toml").is_file())

    def has_dataset(self, dataset_id: str) -> bool:
        return (self.dataset_dir(dataset_id) / "contract.toml").is_file()

    def require_dataset(self, dataset_id: str) -> None:
        self.require()
        if not self.has_dataset(dataset_id):
            raise NotFoundError(f"no dataset {dataset_id!r} in {self.root} (run `pallet describe`)")

    def load_contract(self, dataset_id: str) -> Contract:
        self.require_dataset(dataset_id)
        path = self.dataset_dir(dataset_id) / "contract.toml"
        text = path.read_text(encoding="utf-8")
        return parse_contract(text, origin=f"{dataset_id}/contract.toml")

    def registration(self, dataset_id: str) -> dict[str, Any]:
        path = self.dataset_dir(dataset_id) / "registration.json"
        return read_json(path) if path.exists() else {}

    def register(self, contract: Contract, base_dir: Path | None) -> bool:
        """Write the contract. Returns False when the same bytes were already registered."""
        folder = self.dataset_dir(contract.id)
        path = folder / "contract.toml"
        if path.exists() and path.read_text(encoding="utf-8") == contract.text:
            return False
        atomic_write(path, contract.text.encode("utf-8"))
        atomic_write(folder / "registration.json", canonical_json({
            "dataset_id": contract.id,
            "contract_sha256": contract.sha256,
            "described_at": now_iso(),
            "base_dir": str(base_dir) if base_dir else None,
            "tool": TOOL,
        }))
        issues = folder / "issues.toml"
        if not issues.exists():
            atomic_write(issues, ISSUES_HEADER.encode("utf-8"))
        return True

    # -- issues ---------------------------------------------------------------
    def added_issues(self, dataset_id: str) -> list[dict[str, Any]]:
        path = self.dataset_dir(dataset_id) / "issues.toml"
        if not path.exists():
            return []
        return parse_issues_file(path.read_text(encoding="utf-8"), f"{dataset_id}/issues.toml")

    def issues(self, dataset_id: str, contract: Contract | None = None) -> list[dict[str, Any]]:
        contract = contract or self.load_contract(dataset_id)
        out = []
        for issue in contract.issues:
            out.append({**issue, "origin": "contract"})
        for issue in self.added_issues(dataset_id):
            out.append({**issue, "origin": "issues.toml"})
        return out

    def add_issue(self, dataset_id: str, issue: dict[str, Any]) -> dict[str, Any]:
        contract = self.load_contract(dataset_id)
        validate_issue(issue)
        existing = {i["id"] for i in self.issues(dataset_id, contract)}
        if issue["id"] in existing:
            raise SpecError(f"issue {issue['id']!r} already exists for {dataset_id}")
        added = self.added_issues(dataset_id) + [issue]
        text = ISSUES_HEADER + "\n" + tomlw.dumps({"issues": added})
        parse_issues_file(text)  # round-trip check before writing
        atomic_write(self.dataset_dir(dataset_id) / "issues.toml", text.encode("utf-8"))
        return issue

    # -- lifecycle --------------------------------------------------------------
    def lifecycle(self, dataset_id: str) -> list[dict[str, Any]]:
        return read_jsonl(self.dataset_dir(dataset_id) / "lifecycle.jsonl")

    def state(self, dataset_id: str) -> str | None:
        events = self.lifecycle(dataset_id)
        return events[-1]["state"] if events else None

    def record_state(self, dataset_id: str, state: str, **detail: Any) -> None:
        if state not in STATES:
            raise ValueError(state)
        event = {"state": state, "at": now_iso(), **detail}
        append_line(self.dataset_dir(dataset_id) / "lifecycle.jsonl", compact_json(event))

    def require_state(self, dataset_id: str, allowed: tuple[str, ...], action: str) -> str:
        state = self.state(dataset_id)
        if state not in allowed:
            raise PreconditionError(
                f"cannot {action} {dataset_id}: its lifecycle state is {state or 'unregistered'}; "
                f"needs one of {', '.join(allowed)}",
                state=state,
            )
        return state

    # -- releases -----------------------------------------------------------------
    def releases(self, dataset_id: str) -> list[str]:
        folder = self.dataset_dir(dataset_id) / "releases"
        if not folder.is_dir():
            return []
        return sorted(p.name for p in folder.iterdir()
                      if p.is_dir() and (p / "manifest.json").is_file())

    def current_path(self, dataset_id: str) -> Path:
        return self.dataset_dir(dataset_id) / "current.json"

    def current(self, dataset_id: str) -> dict[str, Any] | None:
        path = self.current_path(dataset_id)
        return read_json(path) if path.exists() else None

    @contextlib.contextmanager
    def lock(self, dataset_id: str) -> Iterator[None]:
        folder = self.dataset_dir(dataset_id)
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / ".lock", "a+") as handle:
            if fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise PreconditionError(
                        f"{dataset_id} is locked by another pallet run"
                    ) from exc
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
