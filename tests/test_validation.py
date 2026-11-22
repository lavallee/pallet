"""The shipped JSON Schemas, the built-in validator, and spec checks."""

from __future__ import annotations

import json

import pytest

from pallet.errors import SpecError
from pallet.spec import find_line, load_contract, parse_contract
from pallet.validation import SCHEMA_NAMES, SUPPORTED_KEYWORDS, errors, load_schema
from tests.conftest import EXAMPLE, spec_text


def _keywords(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            found.add(k)
            if k in ("properties", "$defs"):
                for sub in v.values():
                    _keywords(sub, found)
            elif k not in ("enum", "const", "required", "default", "examples"):
                _keywords(v, found)
    elif isinstance(node, list):
        for item in node:
            _keywords(item, found)


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schemas_use_only_supported_keywords(name):
    found: set[str] = set()
    _keywords(load_schema(name), found)
    assert found <= SUPPORTED_KEYWORDS, found - SUPPORTED_KEYWORDS


@pytest.mark.parametrize("name", SCHEMA_NAMES)
def test_schemas_are_valid_2020_12(name):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(load_schema(name))


def test_full_validator_agrees(released, store, tmp_path):
    jsonschema = pytest.importorskip("jsonschema")
    from pallet.release import make_pin

    folder = store.release_dir("heat-visits", released["release_id"])
    docs = {
        "manifest": json.loads((folder / "manifest.json").read_text()),
        "pin": make_pin(store, "heat-visits"),
        "receipt": store.receipts("heat-visits")[0],
        "contract": load_contract(EXAMPLE / "heat-visits.toml").doc,
    }
    for name, doc in docs.items():
        validator = jsonschema.Draft202012Validator(load_schema(name))
        assert errors(doc, name) == []
        assert list(validator.iter_errors(doc)) == []
        broken = dict(doc)
        broken["unexpected"] = 1
        assert errors(broken, name) and list(validator.iter_errors(broken))


def test_contract_semantics_are_checked():
    eds = '[[source.editions]]\nlabel = "a"\nurl = "https://x.example/a"'
    field = '[[shape.fields]]\nname = "n"\ntype = "integer"'
    parse_contract(spec_text(editions=eds, fields=field))
    with pytest.raises(SpecError, match="both suppressed and unknown"):
        parse_contract(spec_text(editions=eds, fields=field, shape_extra=(
            '[shape.missing]\nsuppressed = ["*"]\nunknown = ["*"]')))
    with pytest.raises(SpecError, match="key field"):
        parse_contract(spec_text(editions=eds, fields=field, shape_extra='key = ["nope"]'))
    with pytest.raises(SpecError, match="redistribute_to"):
        parse_contract(spec_text(editions=eds, fields=field, redistribution="restricted"))
    with pytest.raises(SpecError, match="unexpected key 'colour'"):
        parse_contract(spec_text(editions=eds, fields=field + '\ncolour = "red"'))
    with pytest.raises(SpecError, match="not one of"):
        parse_contract(spec_text(editions=eds, fields=field, redistribution="maybe"))
    with pytest.raises(SpecError, match="unique"):
        parse_contract(spec_text(editions=eds + "\n" + eds, fields=field))


def test_find_line():
    text = '[dataset]\nredistribution = "x"\n\n[rights]\nlicense = "y"\nredistribution = "z"\n'
    assert find_line(text, "rights", "redistribution") == 6
    assert find_line(text, "rights", "missing") is None


def test_describe_check_and_bad_spec(run, tmp_path):
    out = run("describe", str(EXAMPLE / "heat-visits.toml"), "--check")
    assert out["valid"] is True and out["registered"] is False
    bad = tmp_path / "bad.toml"
    bad.write_text("[pallet\n")
    out = run("describe", str(bad), expect=3)
    assert out["error"]["kind"] == "invalid"


def test_store_must_be_initialised(tmp_path, capsys):
    from pallet.cli import main

    code = main(["status", "--store", str(tmp_path / "nothing"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 7 and out["error"]["kind"] == "precondition"
