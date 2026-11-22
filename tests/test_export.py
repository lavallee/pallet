"""The Frictionless export and the plain-language README."""

from __future__ import annotations

import csv
import json

from pallet.package import check_datapackage
from pallet.validation import errors


def test_export_is_a_valid_data_package(released, run, tmp_path):
    exp = tmp_path / "exp"
    out = run("export", "heat-visits", "--to", str(exp))
    assert out["not_for_redistribution"] is False
    assert {"datapackage.json", "data.csv", "README.md", "pin.json"} <= set(out["files"])
    assert check_datapackage(exp) == []

    package = json.loads((exp / "datapackage.json").read_text())
    assert package["profile"] == "tabular-data-package"
    resource = package["resources"][0]
    schema = resource["schema"]
    assert schema["missingValues"] == [""]
    assert schema["primaryKey"] == ["year", "district_code"]
    fields = {f["name"]: f for f in schema["fields"]}
    assert fields["heat_visits"]["unit"] == "visits"
    assert fields["visit_rate"]["unit"] == "visits per 10,000 residents"
    assert fields["heat_visits"]["description"]
    assert fields["heat_visits__state"]["constraints"]["enum"][0] == "reported"
    assert "district__state" not in fields  # every district name was reported
    with open(exp / "data.csv", newline="") as handle:
        assert next(csv.reader(handle)) == [f["name"] for f in schema["fields"]]
    assert {i["id"] for i in package["pallet"]["issues"]} == {
        "visit-definition-2024", "lakeshore-split-2024"}
    assert package["pallet"]["rights"]["redistribution"] == "allowed"

    pin = json.loads((exp / "pin.json").read_text())
    assert errors(pin, "pin") == []
    # A public export is byte-identical to the release, so it verifies against its pin.
    assert run("verify", "--pin", str(exp / "pin.json"), "--dir", str(exp))["ok"]


def test_readme_carries_rights_issues_and_states(released, run, tmp_path):
    exp = tmp_path / "exp"
    run("export", "heat-visits", "--to", str(exp))
    readme = (exp / "README.md").read_text()
    assert "## Rights" in readme and "CC0-1.0" in readme
    assert "Prohibited use: Do not present heat_visits as a count of people" in readme
    assert "## Known issues" in readme
    assert "The 2024 edition counts visits by a broader rule" in readme
    assert '"Beginning with the 2024 report' in readme and "2024-06-01" in readme
    assert "`suppressed`" in readme and "never a zero" in readme


def test_check_datapackage_finds_problems(released, run, tmp_path):
    exp = tmp_path / "exp"
    run("export", "heat-visits", "--to", str(exp))
    with open(exp / "data.csv", "a") as handle:
        handle.write("2025,derived,X,XX,lots,reported,,,,,,,,\n")
    package = json.loads((exp / "datapackage.json").read_text())
    package["resources"][0]["schema"]["fields"][0]["type"] = "spreadsheet"
    (exp / "datapackage.json").write_text(json.dumps(package))
    problems = check_datapackage(exp)
    assert any("hash does not match" in p for p in problems)
    assert any("spreadsheet" in p for p in problems)
    assert any("'lots' is not integer" in p for p in problems)


def test_issues_ship_in_every_release(released, run, store):
    run("issue", "add", "heat-visits", "--title", "Suppression threshold is 5",
        "--effect", "Counts below 5 are withheld, and so are rates computed from them.",
        "--misuse", "Treating a suppressed district as having no visits.",
        "--instead", "Say the figure was withheld.", "--field", "heat_visits",
        "--quote", "Counts below 5, and rates based on them, are suppressed and shown as *.",
        "--quote-source", "publisher/index.html", "--quote-date", "2024-06-01")
    second = run("release", "heat-visits")
    issues = json.loads(
        (store.release_dir("heat-visits", second["release_id"]) / "issues.json").read_text())
    assert [i["id"] for i in issues["issues"]][-1] == "suppression-threshold-is-5"
    assert issues["issues"][-1]["origin"] == "issues.toml"
    # The earlier release is untouched.
    first = json.loads(
        (store.release_dir("heat-visits", released["release_id"]) / "issues.json").read_text())
    assert len(first["issues"]) == 2


def test_issue_with_quote_needs_a_date(released, run):
    out = run("issue", "add", "heat-visits", "--title", "t", "--effect", "e",
              "--quote", "words", expect=3)
    assert "quote-date" in out["error"]["message"]
    run("issue", "add", "heat-visits", "--id", "dup", "--title", "t", "--effect", "e")
    run("issue", "add", "heat-visits", "--id", "dup", "--title", "t", "--effect", "e", expect=3)
