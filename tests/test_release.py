"""Releases: atomic promotion, verification, pins, materialize, rollback, rights."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from pallet import release as release_mod
from pallet.errors import RefusedError, VerifyError
from pallet.release import make_pin, materialize, verify_pin
from tests.conftest import ROOT


def test_release_layout_and_manifest(released, store):
    rid = released["release_id"]
    folder = store.release_dir("heat-visits", rid)
    names = sorted(p.name for p in folder.iterdir())
    assert names == ["README.md", "contract.toml", "data.csv", "datapackage.json",
                     "issues.json", "manifest.json"]
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["release_id"] == rid
    assert [i["edition"] for i in manifest["inputs"]] == ["2023", "2024"]
    assert manifest["note"] == "first"
    current = store.current("heat-visits")
    assert current["release_id"] == rid and current["lineage"] == [rid]
    assert store.state("heat-visits") == "released"
    assert not (store.dataset_dir("heat-visits") / ".staging").exists()


def test_unchanged_release_makes_nothing_new(released, run):
    again = run("release", "heat-visits")
    assert again["status"] == "unchanged"
    assert again["release_id"] == released["release_id"]


def _second_release(run):
    run("issue", "add", "heat-visits", "--title", "Rates use old population estimates",
        "--effect", "Rates use the prior year's population.", "--severity", "info")
    return run("release", "heat-visits", "--note", "second")


def test_failed_promotion_leaves_current_untouched(released, run, store, monkeypatch):
    before = store.current_path("heat-visits").read_bytes()
    run("issue", "add", "heat-visits", "--title", "x issue", "--effect", "x")

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(release_mod, "_write_pointer", boom)
    with pytest.raises(RuntimeError):
        release_mod.build_release(store, "heat-visits")
    assert store.current_path("heat-visits").read_bytes() == before
    assert store.releases("heat-visits") == [released["release_id"]]
    assert not (store.dataset_dir("heat-visits") / ".staging").exists()
    run("verify", "heat-visits")


def test_kill_between_build_and_promote(released, run, store, tmp_path):
    """A process killed after building but before promoting changes nothing visible."""
    run("issue", "add", "heat-visits", "--title", "y issue", "--effect", "y")
    before = store.current_path("heat-visits").read_bytes()
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(ROOT / "src")!r})
        from pallet import release
        from pallet.store import Store
        def die(*args, **kwargs):
            os._exit(137)  # like SIGKILL: no cleanup, no finally blocks
        release._promote = die
        release.build_release(Store({str(store.root)!r}), "heat-visits")
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == 137
    assert store.current_path("heat-visits").read_bytes() == before
    assert store.releases("heat-visits") == [released["release_id"]]
    staging = store.dataset_dir("heat-visits") / ".staging"
    assert staging.exists()  # the half-built candidate was left behind...
    run("verify", "heat-visits")  # ...but the current release is intact
    second = run("release", "heat-visits")  # and the next run cleans it up
    assert second["status"] == "released"
    assert not staging.exists()


def test_tampering_with_a_released_artifact_fails_verify(released, run, store):
    data = store.release_dir("heat-visits", released["release_id"]) / "data.csv"
    os.chmod(data, 0o644)
    data.write_text(data.read_text().replace(",41,", ",14,"))
    out = run("verify", "heat-visits", expect=5)
    assert out["error"]["kind"] == "verify"
    assert "data.csv does not match" in out["error"]["message"] + json.dumps(out)
    with pytest.raises(VerifyError):
        make_pin(store, "heat-visits")


def test_extra_file_in_release_fails_verify(released, run, store):
    folder = store.release_dir("heat-visits", released["release_id"])
    (folder / "sneaky.csv").write_text("x")
    run("verify", "heat-visits", expect=5)


def test_pin_materialize_verify_offline(released, run, store, tmp_path, monkeypatch):
    pin = make_pin(store, "heat-visits")
    assert pin["manifest_sha256"] == released["manifest_sha256"]
    assert {a["path"] for a in pin["artifacts"]} >= {"data.csv", "datapackage.json"}
    pin_file = tmp_path / "pin.json"
    pin_file.write_text(json.dumps(pin))

    import socket

    def no_socket(*args, **kwargs):
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", no_socket)
    out = tmp_path / "copy"
    report = run("materialize", "--pin", str(pin_file), "--to", str(out))
    assert report["ok"] is True and report["not_for_redistribution"] is False
    assert (out / "pin.json").exists()
    assert run("verify", "--pin", str(pin_file), "--dir", str(out))["ok"] is True

    # The copy is self-sufficient: delete the store and it still verifies.
    import shutil

    shutil.rmtree(store.root)
    assert verify_pin(pin, directory=out)["ok"] is True
    (out / "data.csv").write_text("tampered\n")
    with pytest.raises(VerifyError, match="data.csv"):
        verify_pin(pin, directory=out)


def test_materialize_refuses_existing_nonempty_target(released, store, tmp_path):
    target = tmp_path / "busy"
    target.mkdir()
    (target / "keep.txt").write_text("mine")
    with pytest.raises(Exception, match="not an empty directory"):
        materialize(store, make_pin(store, "heat-visits"), target)
    assert (target / "keep.txt").read_text() == "mine"


def test_rollback(released, run, store):
    first = released["release_id"]
    second = _second_release(run)
    assert second["status"] == "released"
    assert store.current("heat-visits")["release_id"] == second["release_id"]
    back = run("rollback", "heat-visits")
    assert back["release_id"] == first
    assert store.current("heat-visits")["release_id"] == first
    assert store.current("heat-visits")["lineage"] == [first]
    assert store.releases("heat-visits") == sorted([first, second["release_id"]])
    run("verify", "heat-visits")
    out = run("rollback", "heat-visits", expect=7)
    assert "no earlier release" in out["error"]["message"]
    # A pin to the rolled-back release still verifies: releases are never deleted.
    pin = make_pin(store, "heat-visits", second["release_id"])
    assert verify_pin(pin, store=store)["ok"]


def _restrict(example, run, value="prohibited", extra=""):
    spec = example / "heat-visits.toml"
    text = spec.read_text().replace('redistribution = "allowed"',
                                    f'redistribution = "{value}"{extra}')
    spec.write_text(text)
    run("describe", str(spec))
    run("capture", "heat-visits")
    run("shape", "heat-visits")
    run("release", "heat-visits")
    return text


def test_rights_refusal_names_the_contract_line(run, example, store, tmp_path):
    text = _restrict(example, run)
    line = next(n for n, ln in enumerate(text.splitlines(), 1)
                if ln.startswith("redistribution ="))
    out = run("export", "heat-visits", "--to", str(tmp_path / "exp"), expect=6)
    assert out["error"]["kind"] == "refused"
    assert f"contract.toml line {line}" in out["error"]["message"]
    assert 'redistribution = "prohibited"' in out["error"]["message"]
    assert out["error"]["detail"]["contract_line"] == line
    assert not (tmp_path / "exp").exists()

    pin_file = tmp_path / "pin.json"
    pin_file.write_text(json.dumps(make_pin(store, "heat-visits")))
    out = run("materialize", "--pin", str(pin_file), "--to", str(tmp_path / "m"), expect=6)
    assert f"line {line}" in out["error"]["message"]
    with pytest.raises(RefusedError):
        materialize(store, json.loads(pin_file.read_text()), tmp_path / "m2")


def test_private_copies_are_marked(run, example, store, tmp_path):
    _restrict(example, run, "restricted", '\nredistribute_to = ["the newsroom"]')
    exp = tmp_path / "exp"
    out = run("export", "heat-visits", "--to", str(exp), "--private")
    assert out["not_for_redistribution"] is True
    assert (exp / "NOT-FOR-REDISTRIBUTION.txt").exists()
    package = json.loads((exp / "datapackage.json").read_text())
    assert package["pallet"]["not_for_redistribution"] is True
    assert package["description"].startswith("NOT FOR REDISTRIBUTION")
    assert "NOT FOR REDISTRIBUTION" in (exp / "README.md").read_text()

    pin_file = tmp_path / "pin.json"
    pin_file.write_text(json.dumps(make_pin(store, "heat-visits")))
    m = tmp_path / "m"
    report = run("materialize", "--pin", str(pin_file), "--to", str(m), "--private")
    assert report["not_for_redistribution"] is True
    assert (m / "NOT-FOR-REDISTRIBUTION.txt").exists()
    run("verify", "--pin", str(pin_file), "--dir", str(m))


def test_release_requires_a_fresh_shape(released, run, example):
    (example / "publisher" / "2024.csv").write_text(
        (example / "publisher" / "2024.csv").read_text().replace(",58,", ",59,"))
    run("capture", "heat-visits")
    out = run("release", "heat-visits", expect=7)
    assert "lifecycle state is captured" in out["error"]["message"]


def test_deprecated_dataset_refuses_release_but_pins_verify(released, run, store, tmp_path):
    pin_file = tmp_path / "pin.json"
    pin_file.write_text(json.dumps(make_pin(store, "heat-visits")))
    run("deprecate", "heat-visits", "--reason", "publisher withdrew the series")
    run("release", "heat-visits", expect=7)
    run("verify", "--pin", str(pin_file))
