# Changelog

All notable changes to pallet are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [0.1.0] - unreleased

First version.

### Added

- A store on disk with write-once raw custody under SHA-256, append-only
  capture receipts, per-dataset contracts, issues, and an explicit lifecycle
  (described, captured, shaped, released, deprecated).
- Declarative TOML dataset specs: editions, index page, format (CSV, TSV,
  JSON Lines), rights, shaping (columns, renames across editions, types,
  units, descriptions, missing-value tokens, key, filters), and known issues.
- Shaping with typed value states: reported, derived, suppressed,
  not_collected, not_applicable, parse_failure, unknown.
- Immutable releases with a manifest of hashes, verified before and after an
  atomic pointer move; rollback.
- Consumer pins, offline verification, and materialize.
- Frictionless Data Package export with `missingValues`, per-field `unit` and
  `description`, state columns, and a plain-language README carrying rights
  and issues.
- Rights enforcement on export and materialize, naming the contract line;
  `--private` copies are marked not for redistribution.
- `adopt`: capture one CSV or TSV and infer a draft spec for review.
- JSON Schemas for contract, receipt, manifest, and pin, shipped in the
  package.
- The `pallet` CLI with `--json` on every command and documented exit codes.
- A worked example (`examples/heat-visits/`) with two editions and a
  definition change recorded as an issue.
