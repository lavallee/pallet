"""The worked example, end to end, through the command line only."""

from __future__ import annotations

import csv
import json
import subprocess
import sys

from pallet.cli import main
from pallet.package import check_datapackage
from tests.conftest import ROOT


def cli(capsys, *argv, expect=0):
    code = main(list(argv))
    captured = capsys.readouterr()
    assert code == expect, f"{argv}: exit {code}\n{captured.out}\n{captured.err}"
    return captured.out


def test_worked_example_end_to_end(example, tmp_path, capsys, monkeypatch):
    store = tmp_path / "store"
    monkeypatch.setenv("PALLET_STORE", str(store))
    spec = example / "heat-visits.toml"

    assert "Created pallet store" in cli(capsys, "init")
    cli(capsys, "describe", str(spec))
    out = json.loads(cli(capsys, "capture", "heat-visits", "--json"))
    assert [r["status"] for r in out["results"]] == ["new", "new", "new"]  # index + 2 editions
    shaped = cli(capsys, "shape", "heat-visits")
    assert "heat_alert_days" in shaped and "not_collected" in shaped
    first = json.loads(cli(capsys, "release", "heat-visits", "--note", "2023 and 2024", "--json"))
    assert first["status"] == "released" and first["verified"] is True

    # The table: suppressed cells are empty with a reason, never zero.
    release_dir = store / "datasets" / "heat-visits" / "releases" / first["release_id"]
    with open(release_dir / "data.csv", newline="") as handle:
        rows = list(csv.DictReader(handle))
    old_mill_2023 = next(r for r in rows if r["district_code"] == "OM" and r["year"] == "2023")
    assert old_mill_2023["heat_visits"] == ""
    assert old_mill_2023["heat_visits__state"] == "suppressed"
    lakeshore_2023 = next(r for r in rows if r["district_code"] == "LK" and r["year"] == "2023")
    assert lakeshore_2023["population__state"] == "not_applicable"

    show = json.loads(cli(capsys, "show", "heat-visits", "--json"))
    assert show["state"] == "released"
    assert show["rights"]["redistribution"] == "allowed"
    assert {i["id"] for i in show["issues"]} == {"visit-definition-2024", "lakeshore-split-2024"}
    assert "The 2024 edition counts visits by a broader rule" in cli(capsys, "show", "heat-visits")

    # A reporter pins the release they used.
    pin_text = cli(capsys, "pin", "heat-visits")
    pin_file = tmp_path / "pin.json"
    pin_file.write_text(pin_text)
    pin = json.loads(pin_text)
    assert pin["release_id"] == first["release_id"]
    assert str(tmp_path) not in pin_text  # no machine paths in a pin

    # The publisher quietly revises 2024. The next capture shows drift.
    edition = example / "publisher" / "2024.csv"
    edition.write_text(edition.read_text().replace("Riverside,RS,\"25,110\",91,36.2",
                                                   "Riverside,RS,\"25,110\",93,37.0"))
    cli(capsys, "capture", "heat-visits")
    status = json.loads(cli(capsys, "status", "--json"))["datasets"][0]
    ed2024 = next(e for e in status["editions"] if e["edition"] == "2024")
    assert ed2024["drift"] is True and ed2024["release_behind"] is True
    assert "DRIFT" in cli(capsys, "status")

    # Record what we learned, reshape offline, release again.
    cli(capsys, "issue", "add", "heat-visits", "--id", "riverside-revised-2024",
        "--title", "2024 Riverside figures were revised after publication",
        "--effect", "The publisher replaced the 2024 file; Riverside rose from 91 to 93 visits.",
        "--instead", "Cite the release you used, by pin.", "--edition", "2024",
        "--field", "heat_visits")
    cli(capsys, "capture", "heat-visits", "--offline")
    cli(capsys, "shape", "heat-visits")
    second = json.loads(cli(capsys, "release", "heat-visits", "--json"))
    assert second["previous_release"] == first["release_id"]

    # The old pin still verifies against the store, and against an offline copy.
    cli(capsys, "verify", "--pin", str(pin_file))
    copy = tmp_path / "copy"
    cli(capsys, "materialize", "--pin", str(pin_file), "--to", str(copy))
    cli(capsys, "verify", "--pin", str(pin_file), "--dir", str(copy))
    with open(copy / "data.csv", newline="") as handle:
        riverside = next(r for r in csv.DictReader(handle)
                         if r["district_code"] == "RS" and r["year"] == "2024")
    assert riverside["heat_visits"] == "91"  # the pinned number, not the revised one

    # Export the current release for publication.
    exp = tmp_path / "publish"
    cli(capsys, "export", "heat-visits", "--to", str(exp))
    assert check_datapackage(exp) == []
    assert "2024 Riverside figures were revised" in (exp / "README.md").read_text()

    # Roll back, and verify everything.
    back = json.loads(cli(capsys, "rollback", "heat-visits", "--json"))
    assert back["release_id"] == first["release_id"]
    report = json.loads(cli(capsys, "verify", "heat-visits", "--json"))
    assert report["ok"] is True and len(report["releases"]) == 2


def test_console_entry_point_runs(tmp_path):
    proc = subprocess.run([sys.executable, "-m", "pallet", "--version"], capture_output=True,
                          text=True, cwd=ROOT)
    assert proc.returncode == 0 and proc.stdout.strip() == "pallet/0.1.0"
    proc = subprocess.run([sys.executable, "-m", "pallet", "bogus"], capture_output=True,
                          text=True, cwd=ROOT)
    assert proc.returncode == 2
