"""Capture: put the publisher's bytes into custody, with a receipt for every retrieval."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pallet import TOOL, fetch
from pallet.errors import FetchError, PreconditionError, VerifyError
from pallet.store import Store, safe_filename
from pallet.util import compact_json, now_iso, sha256_bytes


def resolve_url(url: str, base_dir: str | None) -> str:
    """Turn a bare path in a spec into a file URL, relative to the spec's directory."""
    if urlparse(url).scheme:
        return url
    path = Path(url)
    if not path.is_absolute():
        if not base_dir:
            raise PreconditionError(f"relative source path {url!r} but no spec directory recorded")
        path = Path(base_dir) / path
    return path.resolve().as_uri()


def make_receipt(
    dataset_id: str, role: str, edition: str | None, result: fetch.FetchResult,
    sha: str, stored_as: str, new_object: bool,
) -> dict[str, Any]:
    receipt = {
        "pallet_receipt": 1,
        "dataset_id": dataset_id,
        "role": role,
        "edition": edition,
        "url": result.url,
        "final_url": result.final_url,
        "retrieved_at": now_iso(),
        "http_status": result.status,
        "headers": dict(sorted(result.headers.items())),
        "bytes": len(result.body),
        "sha256": sha,
        "stored_as": stored_as,
        "new_object": new_object,
        "tool": TOOL,
    }
    salt = os.urandom(8).hex()
    receipt["receipt_id"] = "rcpt-" + sha256_bytes((compact_json(receipt) + salt).encode())[:16]
    return receipt


def retrieve(store: Store, dataset_id: str, role: str, edition: str | None, url: str):
    """Fetch one URL into custody and append its receipt. Returns the receipt."""
    result = fetch.fetch_url(url)
    if not 200 <= result.status < 300:
        raise FetchError(f"{url} answered HTTP {result.status}", url=url, http_status=result.status)
    sha, stored_as, new = store.put_raw(result.body, safe_filename(result.final_url or url))
    receipt = make_receipt(dataset_id, role, edition, result, sha, stored_as, new)
    store.append_receipt(receipt)
    return receipt


def capture(store: Store, dataset_id: str, offline: bool = False) -> dict[str, Any]:
    contract = store.load_contract(dataset_id)
    store.require_state(dataset_id, ("described", "captured", "shaped", "released"), "capture")
    base_dir = store.registration(dataset_id).get("base_dir")
    targets: list[tuple[str, str | None, str]] = []
    if contract.dataset.get("index_url"):
        targets.append(("index", None, contract.dataset["index_url"]))
    targets += [("edition", e["label"], e["url"]) for e in contract.editions]

    previous = {
        "edition": store.latest_receipts(dataset_id, "edition"),
        "index": store.latest_receipts(dataset_id, "index"),
    }
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for role, label, spec_url in targets:
        url = resolve_url(spec_url, base_dir)
        prior = previous[role].get(label or "")
        entry: dict[str, Any] = {"role": role, "edition": label, "url": url}
        if offline:
            if prior is None:
                failures.append({**entry, "error": "never captured; offline replay has nothing"})
                continue
            try:
                store.read_raw(prior["sha256"])
            except VerifyError as exc:
                failures.append({**entry, "error": exc.message})
                continue
            entry.update(status="replayed", sha256=prior["sha256"], receipt_id=prior["receipt_id"])
            results.append(entry)
            continue
        try:
            receipt = retrieve(store, dataset_id, role, label, url)
        except (FetchError, VerifyError) as exc:
            failures.append({**entry, "error": exc.message})
            continue
        if prior is None:
            change = "new"
        elif prior["sha256"] == receipt["sha256"]:
            change = "unchanged"
        else:
            change = "changed"
        entry.update(
            status=change,
            sha256=receipt["sha256"],
            previous_sha256=prior["sha256"] if prior else None,
            receipt_id=receipt["receipt_id"],
            new_object=receipt["new_object"],
            bytes=receipt["bytes"],
        )
        results.append(entry)

    report = {"dataset_id": dataset_id, "offline": offline, "results": results,
              "failures": failures}
    if failures:
        raise FetchError(
            f"{len(failures)} of {len(targets)} source(s) failed for {dataset_id}: "
            + "; ".join(f"{f['edition'] or f['role']}: {f['error']}" for f in failures),
            **report,
        )
    if not offline:
        store.record_state(
            dataset_id, "captured",
            receipts=[r["receipt_id"] for r in results],
            changed=[r["edition"] or r["role"] for r in results if r["status"] == "changed"],
        )
    return report
