# Disk Data Refresher

Disk Data Refresher safely rewrites every tracked regular file in an existing HDD folder. It supports resumable runs using SQLite state and replaces a source file only after a separately verified temporary copy matches its SHA-256 hash.

This is a Windows-oriented Python project (`pywin32` is a dependency).

## Before you start

- Use the archive folder on the external HDD as the refresh root.
- Do not modify, move, add, or delete files under that root while a run is active.
- Close sync, backup, indexing, and other software that could change its files.
- Leave free HDD space for the largest source file plus the program's 1 GiB safety margin: each file is copied temporarily beside the original.
- Keep the SQLite database on the internal SSD. Its default is `%USERPROFILE%\.refresh\refresh.db`.

## Install on Windows

1. Install Python 3 and open **Command Prompt** in this repository. To change
   drive and directory from another location, use:

   ```bat
   cd /d "D:\Study\Python Scripts\disk-data-refresher"
   ```

2. Create and activate a virtual environment:

   ```bat
   python -m venv .venv
   call .venv\Scripts\activate.bat
   ```

   Your prompt should now begin with `(.venv)`. Run the remaining commands in
   that same Command Prompt window.

3. Install dependencies:

   ```bat
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

4. Verify the installation without touching the HDD:

   ```bat
   python -m pytest
   python refresh_cli.py --help
   ```

## Refresh an HDD

Replace `E:\Archive` with the archive folder on the external HDD.

1. Create a new metadata-only inventory. `--depth 0` creates one job; `1` groups by root-level directory; `2` groups by second-level directory. Root-level files are always included in a root job.

   ```bat
   python refresh_cli.py --db "%USERPROFILE%\.refresh\refresh.db" start E:\Archive --depth 2 --description "2026 HDD refresh"
   ```

   Record the displayed run ID; the examples below use `17`.

2. Inspect the inventory:

   ```bat
   python refresh_cli.py status 17
   python refresh_cli.py jobs 17
   ```

3. Process a deliberately small session first:

   ```bat
   python refresh_cli.py resume 17 --jobs 1
   ```

   `--jobs` is a sequential job limit, not parallel processing. You can instead, or additionally, limit a session by file count, data volume, or duration:

   ```bat
   python refresh_cli.py resume 17 --max-files 100
   python refresh_cli.py resume 17 --max-gb 20
   python refresh_cli.py resume 17 --max-hours 2
   ```

4. Check progress and resume the same run later:

   ```bat
   python refresh_cli.py status 17
   python refresh_cli.py resume 17 --max-hours 2
   ```

   Press `Ctrl+C` to stop at a safe boundary. Resume with the same run ID; do not use `start` to continue an existing run.

5. Failed files require an explicit retry after their cause is fixed:

   ```bat
   python refresh_cli.py failures 17
   python refresh_cli.py retry 17
   python refresh_cli.py resume 17
   ```

6. Confirm final status and view prior runs:

   ```bat
   python refresh_cli.py status 17
   python refresh_cli.py history
   ```

Use `start` for each future maintenance cycle: it creates a new, independent inventory.

## Commands and files

```text
python refresh_cli.py start ROOT --depth N [--description TEXT]
python refresh_cli.py resume RUN_ID [--jobs N] [--max-files N] [--max-gb N] [--max-hours N]
python refresh_cli.py status RUN_ID
python refresh_cli.py jobs RUN_ID
python refresh_cli.py failures RUN_ID
python refresh_cli.py retry RUN_ID
python refresh_cli.py history
```

`resume` accepts `--checkpoint-files N` (default `50`) and `--checkpoint-seconds N` (default `60`). Put global options (`--db`, `--log`, `--verbose`) before the command name. By default the state database is at `%USERPROFILE%\.refresh\refresh.db` and logs are at `%USERPROFILE%\.refresh\logs\refresh.log`.

## Development

```bat
python -m pytest
python -m pytest --cov=. --cov-report=term-missing
```

