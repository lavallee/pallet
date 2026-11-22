"""Custody: write-once raw storage, receipts, drift, offline replay."""

from __future__ import annotations

import os
import stat

import pytest

from pallet import fetch, validation
from pallet.capture import capture
from pallet.errors import FetchError
from pallet.spec import parse_contract
from pallet.store import Store
from tests.conftest import spec_text

URL = "https://data.example.org/table.csv"

SPEC = spec_text(
    editions=f'[[source.editions]]\nlabel = "2024"\nurl = "{URL}"',
    fields='[[shape.fields]]\nname = "a"\ntype = "integer"',
    source_extra="",
)


def describe(store: Store, text: str = SPEC) -> None:
    contract = parse_contract(text)
    store.register(contract, base_dir=None)
    store.record_state(contract.id, "described")


def test_raw_storage_is_write_once(store):
    sha, rel, new = store.put_raw(b"a\n1\n", "table.csv")
    path = store.root / rel
    assert new is True
    assert rel == f"raw/{sha}/table.csv"
    assert not os.access(path, os.W_OK) or os.geteuid() == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o444
    before = (path.stat().st_ino, path.stat().st_mtime_ns)

    sha2, rel2, new2 = store.put_raw(b"a\n1\n", "other-name.csv")
    assert (sha2, rel2, new2) == (sha, rel, False)
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == before
    assert sorted(p.name for p in (store.raw_root / sha).iterdir()) == ["table.csv"]


def test_tampered_raw_object_is_detected(store):
    sha, rel, _ = store.put_raw(b"a\n1\n", "table.csv")
    path = store.root / rel
    os.chmod(path, 0o644)
    path.write_bytes(b"a\n2\n")
    with pytest.raises(Exception, match="does not match its hash"):
        store.put_raw(b"a\n1\n", "table.csv")
    with pytest.raises(Exception, match="does not match its hash"):
        store.read_raw(sha)


def test_identical_bytes_one_object_two_receipts(store, web):
    describe(store)
    web.serve(URL, b"a\n1\n")
    first = capture(store, "t")
    second = capture(store, "t")
    receipts = store.receipts("t")
    assert len(receipts) == 2
    assert receipts[0]["sha256"] == receipts[1]["sha256"]
    assert receipts[0]["receipt_id"] != receipts[1]["receipt_id"]
    assert [r["new_object"] for r in receipts] == [True, False]
    assert len([p for p in store.raw_root.iterdir() if not p.name.startswith(".")]) == 1
    assert first["results"][0]["status"] == "new"
    assert second["results"][0]["status"] == "unchanged"
    for receipt in receipts:
        assert validation.errors(receipt, "receipt") == []
        assert receipt["http_status"] == 200
        assert receipt["headers"]["content-type"] == "text/csv"
        assert receipt["tool"] == "pallet/0.1.0"


def test_changed_upstream_is_a_new_object_and_visible_drift(store, web, run):
    describe(store)
    web.serve(URL, b"a\n1\n")
    capture(store, "t")
    status = run("status")["datasets"][0]
    assert status["drift"] is False

    web.serve(URL, b"a\n1\n2\n")
    report = capture(store, "t")
    assert report["results"][0]["status"] == "changed"
    assert report["results"][0]["previous_sha256"] != report["results"][0]["sha256"]
    objects = [p for p in store.raw_root.iterdir() if not p.name.startswith(".")]
    assert len(objects) == 2
    status = run("status")["datasets"][0]
    assert status["drift"] is True
    edition = status["editions"][0]
    assert edition["drift"] is True and edition["versions"] == 2
    assert store.lifecycle("t")[-1]["changed"] == ["2024"]

    # Capturing again with no further change clears the drift flag but keeps the history.
    capture(store, "t")
    edition = run("status")["datasets"][0]["editions"][0]
    assert edition["drift"] is False and edition["ever_changed"] is True


def test_failed_fetch_does_not_advance_lifecycle(store, web, run):
    describe(store)
    web.serve(URL, b"not found", status=404)
    with pytest.raises(FetchError, match="HTTP 404"):
        capture(store, "t")
    assert store.state("t") == "described"
    assert store.receipts("t") == []
    out = run("capture", "t", expect=4)
    assert out["ok"] is False and out["error"]["kind"] == "fetch"


def test_offline_replays_custody_without_network(store, web, monkeypatch):
    describe(store)
    web.serve(URL, b"a\n1\n")
    capture(store, "t")

    def refuse(url, timeout=60.0):
        raise AssertionError("offline capture touched the network")

    monkeypatch.setattr(fetch, "fetch_url", refuse)
    report = capture(store, "t", offline=True)
    assert report["results"][0]["status"] == "replayed"
    assert len(store.receipts("t")) == 1  # a replay is not a retrieval


def test_offline_without_custody_fails_by_url(store, web):
    describe(store)
    with pytest.raises(FetchError, match="never captured"):
        capture(store, "t", offline=True)
    assert web.requests == []


def test_index_page_is_captured_every_time(store, web):
    index = "https://data.example.org/"
    text = SPEC.replace('cadence = "annual"', f'cadence = "annual"\nindex_url = "{index}"')
    describe(store, text)
    web.serve(URL, b"a\n1\n")
    web.serve(index, b"<html>v1</html>")
    capture(store, "t")
    web.serve(index, b"<html>v2</html>")
    capture(store, "t")
    roles = [r["role"] for r in store.receipts("t")]
    assert roles.count("index") == 2
    assert web.requests.count(index) == 2


def test_file_urls_and_user_agent(tmp_path):
    data = tmp_path / "x.csv"
    data.write_bytes(b"a\n1\n")
    result = fetch.fetch_url(data.as_uri())
    assert result.status == 200 and result.body == b"a\n1\n"
    assert result.headers["content-length"] == "4"
    with pytest.raises(FetchError):
        fetch.fetch_url((tmp_path / "missing.csv").as_uri())
    assert fetch.USER_AGENT == "pallet/0.1.0"
    assert "@" not in fetch.USER_AGENT
