# pallet

Pallet is a small, local-first tool for acquiring datasets with custody. You
describe a source, pallet captures it, keeps the bytes it captured forever,
shapes them into a documented table, and publishes immutable releases a
consumer can pin by hash. When a number from that table appears in a story or
a paper, it can be traced back to the exact release it came from, and that
release back to the bytes the publisher served.

It is for reporters and researchers who need to show their data. It is not a
data warehouse, a pipeline framework, or a database.

The package is named `pallet-data` and installs a `pallet` command. It needs
Python 3.12 or later and has no runtime dependencies.

## Principles

1. **Custody before meaning.** Pallet captures first. It stores the bytes
   exactly as the publisher served them, under their SHA-256, and never
   rewrites them. Each retrieval gets a receipt: the URL, the time, the HTTP
   status, the headers that identify the file, its size and hash, and the
   pallet version. Capturing the same bytes twice gives one stored object and
   two receipts.
2. **Describe a source before relying on it.** A dataset starts as a
   contract: who publishes it, where, how often, under what licence, whether
   it may be redistributed and to whom, which uses are allowed and which are
   not ("do not present visits as a count of people"), and what the table
   means.
3. **Meaning travels with the data.** Every released table carries its
   schema: field names, types, units, descriptions, the geography and time
   basis, and how missing values are shown. Missingness has a reason, and the
   reason is recorded per cell. Only a reported or derived value may hold a
   number. A suppressed value is never a zero.
4. **Releases are immutable and atomic.** A release is a directory of files
   and a manifest of their hashes. Pallet verifies it before it becomes
   current and again after. "Current" is a pointer moved in one step. A failed
   build leaves the current release alone. Rollback moves the pointer back.
5. **Consumers pin.** A pin records the dataset, the release, the manifest
   hash, and every file's hash and size. `pallet verify` proves a local copy
   matches a pin without the network. `pallet materialize` copies a pinned
   release out for offline use.
6. **Known issues are part of the product.** A dataset carries its known
   issues: what is wrong or surprising, what it does to the numbers, the
   mistake it invites, and what to do instead, with the publisher's own words
   quoted and dated. Issues ship in every release and every export.
7. **Harvest in full; conform deliberately.** Capture every edition, and the
   publisher's index page, every time. Shaping is a separate step that reads
   only captured bytes, so it can be redone at any time without the network.
8. **Nothing is inferred from a later state.** A dataset's lifecycle is
   explicit and recorded: described, captured, shaped, released, and possibly
   deprecated. A step that needs an earlier one checks for it rather than
   assuming it happened.

## Five-minute walkthrough

The repository includes a worked example in `examples/heat-visits/`: a small,
invented dataset of heat-related emergency visits in a fictional county, with
two annual editions. Between them the publisher changed how a visit is counted
and split one district in two. Both changes are recorded as known issues.

```console
$ uv tool install .          # or: pip install .   (from a checkout)
$ pallet init
Created pallet store at pallet-store

$ pallet describe examples/heat-visits/heat-visits.toml
heat-visits: contract registered (2 edition(s), 8 field(s), redistribution allowed)

$ pallet capture heat-visits
Captured heat-visits:
  index            new        c3319dd263cd  file:///.../publisher/index.html
  2023             new        964083450f27  file:///.../publisher/2023.csv
  2024             new        def540a12393  file:///.../publisher/2024.csv

$ pallet shape heat-visits
Shaped heat-visits: 10 row(s), data sha256 af8b46d58b47
  heat_visits: not_applicable 1, reported 6, suppressed 3
  heat_alert_days: not_collected 5, reported 5
  edition 2023 lacks: heat_alert_days (recorded as not_collected)
  ...

$ pallet release heat-visits --note "2023 and 2024 editions"
Released heat-visits 20261122T215022Z-14e5409c (10 rows), manifest sha256 d3777f24470f; verified and current

$ pallet show heat-visits            # contract, rights, fields, releases, issues
$ pallet pin heat-visits > pin.json  # record exactly what you used
$ pallet materialize --pin pin.json --to ./heat-visits-copy
$ pallet verify --pin pin.json --dir ./heat-visits-copy
OK: ./heat-visits-copy matches pin heat-visits 20261122T215022Z-14e5409c (6 files checked)

$ pallet export heat-visits --to ./publish   # a Frictionless Data Package
```

Now suppose the publisher quietly replaces the 2024 file. Edit
`examples/heat-visits/publisher/2024.csv` (in a copy) and capture again:

```console
$ pallet capture heat-visits
$ pallet status
heat-visits  [captured]  current: 20261122T215022Z-14e5409c  releases: 1  issues: 2  redistribution: allowed
  2023             captures 2   964083450f27
  2024             captures 2   (new hash)    DRIFT: upstream bytes changed at last capture; newer than current release

$ pallet issue add heat-visits --title "2024 figures revised after publication" \
    --effect "The publisher replaced the 2024 file." --edition 2024
$ pallet shape heat-visits && pallet release heat-visits
$ pallet verify --pin pin.json       # the old pin still verifies: releases are never changed
$ pallet rollback heat-visits        # if the new release is wrong, point back to the old one
```

Both editions' original bytes are still in custody, both receipts are on
record, and the pin taken before the revision still names, and verifies, the
numbers that were used.

`tests/test_e2e.py` runs this whole sequence through the CLI.

## Commands

Every command accepts `--json` (print one JSON object on stdout) and
`--store DIR` (default `$PALLET_STORE`, else `./pallet-store`).

| Command | What it does |
| --- | --- |
| `pallet init` | Create a store. |
| `pallet describe SPEC.toml [--check]` | Validate a spec and register it as the dataset's contract. `--check` validates only. Refuses drafts. |
| `pallet capture DATASET [--offline]` | Fetch the index page and every edition into custody, with receipts. `--offline` fetches nothing: it re-verifies the latest capture of each edition and fails, naming the URL, for any edition never captured. |
| `pallet shape DATASET` | Shape the latest capture of every edition into a typed table (no network). Writes a preview to `datasets/<id>/shaped/`. |
| `pallet release DATASET [--note TEXT]` | Re-shape from custody, build a candidate, verify it, promote it atomically, verify again. Makes nothing if the content is unchanged. |
| `pallet status [DATASET]` | Lifecycle state, current release, captures, upstream drift, and editions newer than the current release. |
| `pallet show DATASET` | Contract, rights, fields, releases, issues, lifecycle history. |
| `pallet verify DATASET` | Verify every release, the current pointer, and the raw custody each release was built from. |
| `pallet verify --pin FILE [--dir DIR]` | Verify the store's copy, or a directory, against a pin. No network. |
| `pallet pin DATASET [--release ID] [--out FILE]` | Print a pin (JSON) for the current or a named release. |
| `pallet materialize --pin FILE --to DIR [--private]` | Copy a pinned release into an empty directory, with `pin.json`, and verify the copy. |
| `pallet rollback DATASET [--to ID]` | Move the current pointer back to the previously promoted release. |
| `pallet deprecate DATASET --reason TEXT` | Mark a dataset deprecated. Releases and pins still verify; no new releases. |
| `pallet issue add DATASET --title ... --effect ... [...]` | Record a known issue (`--misuse`, `--instead`, `--field`, `--edition`, `--quote`, `--quote-source`, `--quote-date`, `--severity`). It ships in the next release. |
| `pallet issue list DATASET` | List known issues. |
| `pallet export DATASET --to DIR [--release ID] [--private]` | Write a Frictionless Data Package: `datapackage.json`, `data.csv`, `README.md`, plus `issues.json`, `contract.toml`, `manifest.json`, `pin.json`. |
| `pallet adopt FILE-OR-URL --id DATASET [--out FILE]` | The quick path: capture one CSV or TSV and write a draft spec for a person to review. Never describes or releases anything. |

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success. |
| 1 | Unexpected error. |
| 2 | Usage error (bad arguments). |
| 3 | Invalid spec, contract, or issue, or data that does not conform to the spec. |
| 4 | A fetch failed, or custody needed for an offline step is missing. |
| 5 | Verification failed: a hash, size, or structure does not match. |
| 6 | Refused by the dataset's rights. |
| 7 | Precondition not met: no store, wrong lifecycle state, target not empty. |
| 8 | Not found: dataset, release, or file. |

With `--json`, success prints `{"ok": true, ...}` and failure prints
`{"ok": false, "error": {"kind", "code", "message", "detail"}}` on stdout.
`pallet pin` prints the pin document itself, with or without `--json`, so
`pallet pin X > pin.json` always produces a valid pin.

## Rights

`export` and `materialize` refuse a dataset whose contract does not say
`redistribution = "allowed"`. The refusal names the line of the release's own
`contract.toml`:

```console
$ pallet export payroll --to out
pallet: refused: refusing to export payroll release 20261122T…: contract.toml line 21:
redistribution = "restricted". Pass --private for a personal copy marked not for redistribution.
```

With `--private` the copy is made and marked: a `NOT-FOR-REDISTRIBUTION.txt`
file, a banner at the top of the README, and `"not_for_redistribution": true`
in `datapackage.json`. A private export's `datapackage.json` and `README.md`
therefore differ from the release; its `data.csv` still matches the pin. A
private materialized copy is byte-identical to the release apart from the
marker file, so it verifies against its pin.

## The store

```text
pallet-store/
  pallet-store.json                     marker written by `pallet init`
  raw/<sha256>/<filename>               captured bytes: write-once, read-only
  receipts/<dataset>.jsonl              one receipt per retrieval, append-only
  datasets/<dataset>/
    contract.toml                       the registered spec, byte for byte
    registration.json                   its hash and where it was registered from
    issues.toml                         issues added with `pallet issue add`
    lifecycle.jsonl                     described / captured / shaped / released / deprecated
    shaped/{data.csv,shape.json}        the latest shaping, a preview
    releases/<release-id>/
      manifest.json                     hashes and sizes of everything below
      data.csv
      datapackage.json
      README.md                         plain language: rights, issues, fields, sources
      issues.json
      contract.toml                     the contract the release was built under
    current.json                        the pointer: release id, manifest hash, lineage
```

Raw objects are stored by first writing them to a temporary directory and
then renaming that directory to `raw/<sha256>`, which fails if it already
exists; the file is then made read-only. Receipts and lifecycle events are
appended, never edited. Release files are made read-only after promotion.
`current.json` is replaced atomically (write, fsync, rename).

A release id looks like `20261122T215022Z-14e5409c`: the UTC build time and
the first eight hex digits of the release's content hash. The content hash
covers the contract, the captured inputs, the table, and the issues, so
rebuilding unchanged content makes no new release.

## The spec format

A spec is a TOML file. `examples/heat-visits/heat-visits.toml` is a complete
one; the JSON Schema is `src/pallet/schemas/contract.schema.json`.

```toml
[pallet]
spec = 1

[dataset]
id = "heat-visits"                    # lowercase letters, digits, . _ -
title = "..."
description = "..."
publisher = "..."
cadence = "annual, each June"
index_url = "https://publisher.example/reports/"   # captured on every capture
geography = "County district"
time_basis = "Calendar year of the visits"
grain = "One row per district per year"

[rights]
license = "CC0-1.0"
license_url = "https://creativecommons.org/publicdomain/zero/1.0/"
redistribution = "allowed"            # allowed | restricted | prohibited | unknown
redistribute_to = []                  # required when restricted
attribution = "..."
allowed_uses = ["..."]
prohibited_uses = ["Do not present visits as a count of people."]

[source]
format = "csv"                        # csv | tsv | jsonl
encoding = "utf-8"                    # a byte-order mark is tolerated
# delimiter = ";"                     # csv only
# skip_rows = 2                       # preamble rows before the header

[[source.editions]]                   # one per edition; capture fetches them all
label = "2023"
url = "https://publisher.example/2023.csv"   # a bare path is relative to the spec file

[shape]
key = ["year", "district_code"]       # must be unique and present in every row
thousands = ","                       # strip this separator from numbers
# allow_parse_failures = false        # by default an unreadable cell fails shaping

[shape.missing]                       # literal tokens, compared after trimming
suppressed = ["*"]
not_collected = ["NC"]
not_applicable = ["N/A"]
# unknown = ["?"]                     # an empty cell is `unknown` unless listed elsewhere

[[shape.filters]]                     # optional; compares the raw cell text
field = "district_code"
op = "ne"                             # eq | ne | in | not_in
value = "TOTAL"

[[shape.fields]]
name = "year"
derive = "edition"                    # take the value from the edition label
type = "integer"

[[shape.fields]]
name = "heat_visits"
source = ["ED Visits (primary diagnosis)", "ED Visits (any diagnosis)"]  # tried in order
type = "integer"                      # string | integer | number | boolean | date
unit = "visits"
description = "..."
# format = "%m/%d/%Y"                 # for non-ISO dates
# missing = { suppressed = ["*", "<5"] }   # per-field override of a category

[[issues]]
id = "visit-definition-2024"
title = "The 2024 edition counts visits by a broader rule"
severity = "warning"                  # info | caution | warning
effect = "What is wrong and what it does to the numbers."
misuse = "The mistake it invites."
instead = "What to do instead."
fields = ["heat_visits"]
editions = ["2024"]
publisher_quote = "The publisher's words, verbatim."
quote_source = "https://publisher.example/reports/"
quote_date = "2024-06-01"
recorded = "2026-11-22"
```

Shaping is deliberately small: select columns, rename them, type them, map
tokens to value states, filter rows, check a key. It is deterministic: the
same captured bytes and contract always give byte-identical output, and
`release` re-shapes from custody and checks that it does.

## Value states

| State | Has a value? | Meaning |
| --- | --- | --- |
| `reported` | yes | The publisher reported it. |
| `derived` | yes | Pallet set it by a rule in the contract (currently: the edition label). |
| `suppressed` | no | The publisher withheld it. It is not zero. |
| `not_collected` | no | Not collected, including a column absent from an edition. |
| `not_applicable` | no | Cannot exist for this row (a structural absence). |
| `parse_failure` | no | The cell could not be read as the declared type. |
| `unknown` | no | An empty cell, or a token of unknown meaning. |

In `data.csv`, a value that is not `reported` or `derived` is an empty cell.
For each field that has **any** value that is not `reported`, a companion
column `<field>__state` follows it and gives the state of every row. Fields
whose values were all reported have no state column. In `datapackage.json`,
`missingValues` is `[""]`, so standard Frictionless tools read the empty cells
as missing, and each state column has an `enum` constraint listing the states.
A column absent from one edition (because the publisher added it later) is
`not_collected` for that edition's rows.

## For consumers: pin, verify, materialize

A pin is a small JSON file that names exactly one release:

```json
{
  "pallet_pin": 1,
  "dataset_id": "heat-visits",
  "title": "Heat-related emergency department visits by district (fictional)",
  "release_id": "20261122T215022Z-14e5409c",
  "manifest_sha256": "d3777f24…",
  "artifacts": [
    {"path": "data.csv", "sha256": "af8b46d5…", "bytes": 1130},
    {"path": "datapackage.json", "sha256": "…", "bytes": 11839}
  ],
  "redistribution": "allowed",
  "license": "CC0-1.0",
  "issue_ids": ["visit-definition-2024", "lakeshore-split-2024"],
  "pinned_at": "2026-11-22T21:50:24Z",
  "tool": "pallet/0.1.0"
}
```

It holds no machine paths, so it can go in a notebook, a repository, or a
story's methodology note. To use the data:

```console
$ pallet pin heat-visits > pin.json
$ pallet materialize --pin pin.json --to data/heat-visits
$ pallet verify --pin pin.json --dir data/heat-visits     # works with no network and no store
```

Verification checks the manifest's hash against the pin, the manifest's file
list against the pin's, and every file's size and SHA-256. A release
manifest (`schemas/manifest.schema.json`) lists the captured inputs (edition,
URL, hash, size, receipt id, retrieval time), every artifact with its hash,
size, and media type, the contract hash, the previous release, and the
redistribution term.

## Using pallet from another program

Call the CLI with `--json` and branch on the exit code; that is the stable
interface. For a workbench:

- **Acquire:** `pallet describe spec.toml --json`, then `capture`, `shape`,
  `release`, each with `--json`. For a single unfamiliar file, `pallet adopt
  FILE --id ID --json` writes a draft spec that a person must review.
- **Record a pin:** `pallet pin ID` (stdout is the pin) and store it next to
  the analysis; check it later with `pallet verify --pin pin.json`.
- **Show issues and rights:** `pallet show ID --json` returns `rights`,
  `issues`, `fields`, and `releases`. A pinned copy has the same in
  `datapackage.json` (`pallet.rights`, `pallet.issues`) and `README.md`.
- **Publish:** `pallet export ID --to DIR --json`. Exit code 6 means the
  rights forbid it; show the message, which names the contract line.

The JSON Schemas for contracts, receipts, manifests and pins ship inside the
package under `pallet/schemas/`.

## What pallet deliberately does not do

- **No spreadsheets.** XLSX is left out: reading it well (shared strings,
  dates stored as serial numbers, merged header cells, multiple sheets) with
  only the standard library is more than a small tool should promise. Convert
  to CSV and capture both files, or capture the XLSX for custody and shape a
  CSV you made from it, recording that step as an issue.
- **No general transformation language.** No joins, pivots, computed columns,
  or reshaping across editions. Do that downstream, from a pinned release.
- **No scraping or crawling.** Pallet fetches the URLs a spec lists, plus the
  index page. It does not follow links or discover editions.
- **No inference without review.** `adopt` writes a draft marked
  `draft = true`; `describe` refuses it until a person has removed that line.
- **No deletion.** There is no command to delete raw bytes, receipts, or
  releases. Rollback moves a pointer; it does not remove anything.
- **No server, no database, no remote storage.** A store is a directory. Copy
  it, back it up, or put it on a shared drive.
- **No authentication or cookies.** Sources behind a login are out of scope
  for now.
- **No concurrent writers across machines.** A per-dataset file lock protects
  release and rollback on one machine (POSIX only).

## Dependencies

None at runtime. Reading TOML, hashing, HTTP, CSV and JSON all come from the
standard library. The shipped schemas are standard JSON Schema 2020-12;
pallet validates against them with a small built-in validator that covers the
keywords they use (a test checks that the schemas use nothing else). The test
suite additionally uses `jsonschema` to confirm the schemas are valid and that
a full validator agrees with the built-in one; it is a development dependency
only.

## Development

```console
$ uv sync
$ uv run ruff check
$ uv run pytest -q
```

All tests are offline. Every network fetch goes through
`pallet.fetch.fetch_url`, which tests replace, and which also reads `file://`
URLs. Pallet identifies itself as `User-Agent: pallet/<version>` and sends no
contact details.

## Licence

MIT. See `LICENSE`.
