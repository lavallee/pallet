"""adopt: capture one table and infer a draft spec, never releasing it."""

from __future__ import annotations

import tomllib

from pallet.adopt import classify_token
from tests.conftest import FIXTURES


def test_adopt_infers_a_draft_from_a_messy_csv(run, store, tmp_path):
    draft = tmp_path / "widgets.draft.toml"
    out = run("adopt", str(FIXTURES / "messy.csv"), "--id", "widgets", "--out", str(draft))
    assert out["released"] is False
    assert out["skip_rows"] == 2 and out["format"] == "csv" and out["encoding"] == "utf-8"
    assert out["rows"] == 6
    fields = {f["name"]: f for f in out["fields"]}
    assert list(fields) == ["county_name", "fips", "widgets_count", "share", "survey_date",
                            "notes"]
    assert fields["fips"]["type"] == "string"  # leading zeros kept
    assert fields["widgets_count"]["type"] == "integer"
    assert fields["share"]["type"] == "number"
    assert fields["survey_date"]["type"] == "date"
    assert fields["notes"]["type"] == "string"
    assert out["thousands"] is True
    assert out["key"] == "county_name"
    missing = out["missing"]
    assert set(missing["suppressed"]) == {"*", "<5"}
    assert missing["not_applicable"] == ["N/A"]
    assert set(missing["not_collected"]) == {"NA", ".."}
    assert "" in missing["unknown"]
    assert "NA" in out["review_tokens"]

    doc = tomllib.loads(draft.read_text())
    assert doc["pallet"]["draft"] is True
    assert doc["rights"]["redistribution"] == "unknown"
    assert "REVIEW" in draft.read_text()

    # The bytes are in custody, with a receipt, but nothing is described or released.
    receipts = store.receipts("widgets")
    assert [r["role"] for r in receipts] == ["adopt"]
    assert not store.has_dataset("widgets")


def test_describe_refuses_a_draft_until_reviewed(run, store, tmp_path):
    draft = tmp_path / "widgets.draft.toml"
    run("adopt", str(FIXTURES / "messy.csv"), "--id", "widgets", "--out", str(draft))
    out = run("describe", str(draft), expect=3)
    assert "draft = true" in out["error"]["message"]
    assert not store.has_dataset("widgets")

    # A person reviews it: here, only the flag is removed. The inferred shaping must
    # then read every cell of the messy file without a parse failure.
    draft.write_text(draft.read_text().replace("draft = true\n", ""))
    run("describe", str(draft))
    run("capture", "widgets")
    shaped = run("shape", "widgets")
    assert shaped["row_count"] == 6
    counts = shaped["state_counts"]
    assert counts["widgets_count"] == {"reported": 3, "suppressed": 2, "not_collected": 1}
    assert counts["share"]["not_applicable"] == 1
    assert all("parse_failure" not in c for c in counts.values())
    assert store.releases("widgets") == []
    # Its redistribution is still unknown, so it could not be exported even once released.


def test_adopt_tsv_in_a_legacy_encoding(run, tmp_path):
    draft = tmp_path / "d.toml"
    out = run("adopt", str(FIXTURES / "messy.tsv"), "--id", "regions", "--out", str(draft))
    assert out["format"] == "tsv" and out["encoding"] == "cp1252"
    fields = {f["name"]: f for f in out["fields"]}
    assert fields["cases"]["type"] == "integer"
    assert fields["rate"]["type"] == "number"
    assert fields["year"]["type"] == "integer"
    assert set(out["review_tokens"]) == {"x", "-"}
    assert out["key"] == "region"


def test_adopt_will_not_overwrite_or_readopt(run, tmp_path):
    draft = tmp_path / "d.toml"
    draft.write_text("mine")
    out = run("adopt", str(FIXTURES / "messy.csv"), "--id", "w", "--out", str(draft), expect=7)
    assert "--force" in out["error"]["message"]
    assert draft.read_text() == "mine"


def test_token_classification():
    assert classify_token("<10") == "suppressed"
    assert classify_token("(S)") == "suppressed"
    assert classify_token("n/a") == "not_applicable"
    assert classify_token("..") == "not_collected"
    assert classify_token("weird") == "unknown"
