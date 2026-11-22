# AGENTS.md

A short contract for agents (and scripts) that use pallet through its CLI.

## Rules

1. **Capture before you cite.** A number you report must come from a pallet
   release, and that release from captured bytes. Do not quote figures from a
   page you only read; describe the source, `capture` it, `shape`, `release`.
2. **Never edit raw custody.** Do not write, move, or delete anything under
   `raw/`, `receipts/`, or `datasets/*/releases/`. Do not edit `current.json`
   or `lifecycle.jsonl` by hand. If something is wrong, record an issue, fix
   the spec, and make a new release; use `rollback` to point back.
3. **Pins go in anything published.** Any analysis, chart, notebook, or story
   that uses pallet data records the pin (`pallet pin DATASET > pin.json`) and
   names the release id. Verify the pin (`pallet verify --pin pin.json`)
   before publishing.
4. **Respect rights.** If `export` or `materialize` exits 6, the contract
   forbids redistribution. Do not work around it. `--private` is for a
   person's own copy; never publish its output.
5. **Show the issues.** When presenting data, present its known issues
   (`pallet show DATASET --json` -> `issues`) and its prohibited uses
   (`rights.prohibited_uses`). Never fill a suppressed or missing value with
   zero; read the `<field>__state` column.
6. **Drafts need a person.** `pallet adopt` writes a draft spec. Do not remove
   `draft = true` yourself; ask the person who owns the data to review it.

## Calling the CLI

- Always pass `--json`; parse stdout; branch on the exit code (0 ok, 2 usage,
  3 invalid, 4 fetch, 5 verify, 6 refused by rights, 7 precondition,
  8 not found). `pallet --help` lists them.
- Set `PALLET_STORE` or pass `--store`.
- `pallet status --json` shows drift: an edition whose bytes changed upstream
  at the last capture, or that is newer than the current release.

## Working on pallet itself

- Python 3.12+, standard library only at runtime.
- `uv run ruff check` and `uv run pytest -q` must pass. Tests are offline;
  network access goes through `pallet.fetch.fetch_url`, which tests replace.
- Keep shaping small and deterministic. Changing a schema under
  `src/pallet/schemas/` is a contract change: note it in `CHANGELOG.md`.
