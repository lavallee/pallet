"""Shaping: typed value states, tokens, keys, filters, formats."""

from __future__ import annotations

import csv
import io

import pytest

from pallet.errors import SpecError
from pallet.shape import STATES, shape
from pallet.spec import parse_contract
from tests.conftest import spec_text

FIELDS = """
[[shape.fields]]
name = "year"
derive = "edition"
type = "integer"

[[shape.fields]]
name = "place"
source = "Place"
type = "string"

[[shape.fields]]
name = "count"
source = ["Count", "Count (new)"]
type = "integer"

[[shape.fields]]
name = "rate"
source = "Rate"
type = "number"

[[shape.fields]]
name = "extra"
source = "Extra"
type = "integer"
"""

SHAPE = """key = ["year", "place"]
thousands = ","
allow_parse_failures = true

[shape.missing]
suppressed = ["*", "<5"]
not_collected = ["NC"]
not_applicable = ["N/A"]
unknown = ["?"]"""

EDITIONS = """[[source.editions]]
label = "2023"
url = "https://x.example/2023.csv"

[[source.editions]]
label = "2024"
url = "https://x.example/2024.csv"
"""

ED_2023 = b"""Place,Count,Rate
A,"1,200",3.5
B,*,*
C,<5,N/A
D,NC,
E,?,abc
F,0,0
"""

ED_2024 = b"""Place,Count (new),Rate,Extra
A,7,0.5,1
"""


def contract(**overrides):
    params = {"editions": EDITIONS, "shape_extra": SHAPE, "fields": FIELDS} | overrides
    return parse_contract(spec_text(**params))


def rows_by_place(result):
    return {r["place"][0] + r["year"][0]: r for r in result.rows}


def test_every_value_state():
    result = shape(contract(), [("2023", ED_2023), ("2024", ED_2024)])
    rows = rows_by_place(result)
    assert rows["A2023"]["count"] == ("1200", "reported")
    assert rows["A2023"]["year"] == ("2023", "derived")
    assert rows["B2023"]["count"] == ("", "suppressed")
    assert rows["C2023"]["count"] == ("", "suppressed")
    assert rows["C2023"]["rate"] == ("", "not_applicable")
    assert rows["D2023"]["count"] == ("", "not_collected")
    assert rows["D2023"]["rate"] == ("", "unknown")  # an empty cell
    assert rows["E2023"]["count"] == ("", "unknown")  # the ? token
    assert rows["E2023"]["rate"] == ("", "parse_failure")
    assert rows["F2023"]["count"] == ("0", "reported")  # a real zero stays a zero
    # A column absent from an edition is not collected for that edition.
    assert rows["A2023"]["extra"] == ("", "not_collected")
    assert rows["A2024"]["extra"] == ("1", "reported")
    # Renamed header between editions resolves through the alternatives.
    assert rows["A2024"]["count"] == ("7", "reported")
    seen = {state for row in result.rows for _, state in row.values()}
    assert seen == set(STATES)
    assert result.editions[0]["absent_fields"] == ["extra"]


def test_only_reported_or_derived_values_hold_a_value():
    result = shape(contract(), [("2023", ED_2023), ("2024", ED_2024)])
    for row in result.rows:
        for value, state in row.values():
            if state in ("reported", "derived"):
                assert value != ""
            else:
                assert value == "", (value, state)


def test_suppressed_token_never_becomes_zero():
    result = shape(contract(), [("2023", ED_2023), ("2024", ED_2024)])
    data = result.to_csv().decode()
    table = list(csv.DictReader(io.StringIO(data)))
    for row in table:
        if row["count__state"] == "suppressed":
            assert row["count"] == ""
            assert row["count"] != "0"
    suppressed = [r for r in table if r["count__state"] == "suppressed"]
    assert len(suppressed) == 2
    # Even a token that looks numeric to a careless parser is held as suppressed.
    odd = contract(shape_extra=SHAPE.replace('["*", "<5"]', '["*", "<5", "0"]'))
    rows = rows_by_place(shape(odd, [("2023", ED_2023)]))
    assert rows["F2023"]["count"] == ("", "suppressed")


def test_state_columns_only_where_needed():
    result = shape(contract(), [("2023", ED_2023), ("2024", ED_2024)])
    header = result.to_csv().decode().splitlines()[0].split(",")
    assert "place__state" not in header  # every place was reported
    assert header[:3] == ["year", "year__state", "place"]
    assert "count__state" in header and "rate__state" in header


def test_parse_failures_refused_unless_allowed():
    strict = contract(shape_extra=SHAPE.replace("allow_parse_failures = true\n", ""))
    with pytest.raises(SpecError, match="could not be read") as info:
        shape(strict, [("2023", ED_2023)])
    assert info.value.detail["parse_failures"][0]["literal"] == "abc"


def test_duplicate_key_rejected():
    with pytest.raises(SpecError, match="already seen"):
        shape(contract(), [("2023", b"Place,Count,Rate\nA,1,1\nA,2,2\n")])


def test_key_must_have_a_value():
    with pytest.raises(SpecError, match="have no value"):
        shape(contract(), [("2023", b"Place,Count,Rate\n*,1,1\n")])


def test_filters():
    shape_extra = SHAPE + """

[[shape.filters]]
field = "place"
op = "in"
value = ["A", "B"]
"""
    result = shape(contract(shape_extra=shape_extra), [("2023", ED_2023)])
    assert sorted(r["place"][0] for r in result.rows) == ["A", "B"]


def test_tsv_jsonl_and_dates():
    fields = """
[[shape.fields]]
name = "day"
source = "day"
type = "date"

[[shape.fields]]
name = "us_day"
source = "us_day"
type = "date"
format = "%m/%d/%Y"

[[shape.fields]]
name = "ok"
source = "ok"
type = "boolean"
"""
    eds = '[[source.editions]]\nlabel = "x"\nurl = "https://x.example/x"'
    tsv = parse_contract(spec_text(fmt="tsv", editions=eds, fields=fields))
    result = shape(tsv, [("x", b"day\tus_day\tok\n2024-01-31\t02/29/2024\tYes\n")])
    assert result.rows[0] == {"day": ("2024-01-31", "reported"),
                              "us_day": ("2024-02-29", "reported"),
                              "ok": ("true", "reported")}
    jsonl = parse_contract(spec_text(fmt="jsonl", editions=eds, fields=fields))
    data = b'{"day": "2024-01-31", "us_day": null, "ok": false}\n\n'
    result = shape(jsonl, [("x", data)])
    assert result.rows[0]["us_day"] == ("", "unknown")
    assert result.rows[0]["ok"] == ("false", "reported")


def test_shaping_is_deterministic():
    c = contract()
    a = shape(c, [("2023", ED_2023), ("2024", ED_2024)]).to_csv()
    b = shape(c, [("2023", ED_2023), ("2024", ED_2024)]).to_csv()
    assert a == b


def test_bom_and_preamble(tmp_path):
    c = contract(source_extra="skip_rows = 2")
    data = "﻿Table 1. Things\n\nPlace,Count,Rate\nA,1,2\n".encode()
    result = shape(c, [("2023", data)])
    assert result.rows[0]["count"] == ("1", "reported")
