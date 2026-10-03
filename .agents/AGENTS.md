# Agent working guide

## Reading order

1. Read this file.
2. Read [`INDEX.md`](INDEX.md).
3. Read all existing Markdown files in the index folder relevant to the request.
4. Before changing behaviour, read `prompt.txt`, the affected root-level Python modules, and their corresponding tests in `tests/`.

## Project invariants

- Prefer a safe failure over a guess. Do not weaken source validation, temporary-copy verification, or atomic replacement without an explicit user decision.
- SQLite is the operational source of truth and belongs on a local/internal disk, not the HDD being refreshed.
- The source tree is unchanged during an active run. New runs are independent inventories.
- Preserve the single-worker model unless the user explicitly changes it. `--jobs` is a sequential work limit, not concurrency.

## Working conventions

- Keep changes scoped and update tests for behaviour changes.
- Run relevant tests and then `python -m pytest` when practical.
- Store durable context in the appropriate `.agents` folder and update `INDEX.md` whenever its folder structure changes.
