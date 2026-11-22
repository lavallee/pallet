from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path

import pytest

from pallet import fetch
from pallet.cli import main
from pallet.store import Store

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "heat-visits"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every test is offline: any attempt to open a real connection fails loudly."""

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to use the network")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "store")
    s.init()
    return s


@pytest.fixture
def example(tmp_path) -> Path:
    """A private copy of the worked example, so tests can change its 'upstream' files."""
    target = tmp_path / "example"
    shutil.copytree(EXAMPLE, target)
    return target


class Runner:
    def __init__(self, store: Store, capsys):
        self.store = store
        self.capsys = capsys

    def __call__(self, *argv: str, expect: int = 0) -> dict:
        code = main([*argv, "--store", str(self.store.root), "--json"])
        out = self.capsys.readouterr().out
        assert code == expect, f"exit {code} (wanted {expect}): {out}"
        return json.loads(out)


@pytest.fixture
def run(store, capsys) -> Runner:
    return Runner(store, capsys)


@pytest.fixture
def released(run, example) -> dict:
    """The example described, captured, shaped, and released once."""
    run("describe", str(example / "heat-visits.toml"))
    run("capture", "heat-visits")
    run("shape", "heat-visits")
    return run("release", "heat-visits", "--note", "first")


class FakeWeb:
    """Stands in for fetch.fetch_url: serves bytes from a dict, counts requests."""

    def __init__(self):
        self.pages: dict[str, tuple[int, bytes]] = {}
        self.requests: list[str] = []

    def serve(self, url: str, body: bytes, status: int = 200) -> None:
        self.pages[url] = (status, body)

    def __call__(self, url: str, timeout: float = 60.0) -> fetch.FetchResult:
        self.requests.append(url)
        if url not in self.pages:
            raise fetch.FetchError(f"could not retrieve {url}: no route", url=url)
        status, body = self.pages[url]
        return fetch.FetchResult(url=url, final_url=url, status=status, body=body,
                                 headers={"content-type": "text/csv",
                                          "content-length": str(len(body))})


@pytest.fixture
def web(monkeypatch) -> FakeWeb:
    fake = FakeWeb()
    monkeypatch.setattr(fetch, "fetch_url", fake)
    return fake


def write_spec(folder: Path, body: str, name: str = "spec.toml") -> Path:
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return path


MINIMAL_SPEC = """\
[pallet]
spec = 1

[dataset]
id = "{id}"
title = "Test table"
description = "A table for tests."
publisher = "Test publisher"
cadence = "annual"

[rights]
license = "{license}"
redistribution = "{redistribution}"
{rights_extra}

[source]
format = "{fmt}"
{source_extra}

{editions}

[shape]
{shape_extra}

{fields}
"""


def spec_text(
    *, id="t", license="CC0-1.0", redistribution="allowed", rights_extra="", fmt="csv",
    source_extra="", editions="", shape_extra="", fields="",
) -> str:
    return MINIMAL_SPEC.format(
        id=id, license=license, redistribution=redistribution, rights_extra=rights_extra,
        fmt=fmt, source_extra=source_extra, editions=editions, shape_extra=shape_extra,
        fields=fields,
    )
