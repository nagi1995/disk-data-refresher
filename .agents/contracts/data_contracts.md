# Data and command contracts

## Filesystem contract

During an active run, nothing outside the program may modify, move, rename,
create, or delete content below the selected root. Only regular, non-symlink
files are eligible. `.refreshtmp` files and configured system directories are
excluded from inventory.

## Database contract

`refresh_db.py` manages the SQLite tables `metadata`, `refresh_runs`,
`directories`, `files`, `failures`, and `run_checkpoints`. File and directory
state uses `PENDING`, `IN_PROGRESS`, `COMPLETED`, and `FAILED` as applicable.
Initial inventory inserts directories and file metadata in one transaction.
File rows are streamed in batches of 1,000 by default; an inventory error rolls
back all of that run's inventory records.

## CLI contract

Global options (`--db`, `--log`, `--verbose`) precede the command. Available
commands are `start ROOT --depth N`, `resume RUN_ID`, `status RUN_ID`,
`jobs RUN_ID`, `failures RUN_ID`, `retry RUN_ID`, and `history`.

`resume` can apply `--jobs`, `--max-files`, `--max-gb`, `--max-hours`,
`--checkpoint-files`, and `--checkpoint-seconds`. Failed files remain failed
until `retry RUN_ID` explicitly resets them.
