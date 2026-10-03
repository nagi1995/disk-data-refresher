# Project memory

## Purpose

Disk Data Refresher safely refreshes files in place on an existing external
HDD. It is a Windows-oriented Python application with a Command Prompt-focused
README.

## Current implementation map

- `refresh_cli.py` defines `start`, `resume`, `status`, `jobs`, `failures`,
  `retry`, and `history` commands.
- `refresh_app.py` owns run lifecycle operations.
- `inventory.py` creates metadata-only inventory and directory jobs.
- `refresh_engine.py` performs the per-file safe-copy operation.
- `refresh_db.py` owns SQLite schema and state transitions.
- `refresh_controller.py` runs directory jobs and checkpoints progress.
- `refresh_logging.py` writes the rotating event log.
- `tests/` contains the pytest suite; checkpoint and logging tests are in
  `test_checkpoint_logging.py`.

## Operational defaults

- State database: `%USERPROFILE%\.refresh\refresh.db`.
- Event log: `%USERPROFILE%\.refresh\logs\refresh.log`.
- Temporary filename suffix: `.refreshtmp`.
- Temporary-space safety margin: 1 GiB.
- Checkpoint defaults: 50 processed files or 60 seconds.
- Inventory file-metadata batch default: 1,000 rows, committed only after the
  complete inventory succeeds.
