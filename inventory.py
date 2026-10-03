# inventory.py

from __future__ import annotations

import os
import time
from pathlib import Path

from refresh_db import insert_directory, insert_files


DIRECTORY_PENDING = "PENDING"
FILE_PENDING = "PENDING"

ROOT_DIRECTORY = "<ROOT>"

SKIP_DIRS = {
    "$RECYCLE.BIN",
    "System Volume Information",
    "Config.Msi",
}

TEMP_SUFFIX = ".refreshtmp"

DEFAULT_FILE_BATCH_SIZE = 1_000


class InventoryError(RuntimeError):
    """Raised when inventory creation cannot safely continue."""


def directory_depth(relative_path: str) -> int:
    if relative_path == ROOT_DIRECTORY:
        return 0

    return len(Path(relative_path).parts)


def build_inventory(
    conn,
    run_id: int,
    root: Path,
    depth: int,
    file_batch_size: int = DEFAULT_FILE_BATCH_SIZE,
) -> dict[str, int]:
    """Create one run inventory, writing file metadata in bounded batches.

    The complete inventory is committed atomically. A failed traversal or
    database write rolls back all directory and file records for this run.
    """

    root = root.resolve()

    if not root.exists():
        raise InventoryError(
            f"Root does not exist: {root}"
        )

    if not root.is_dir():
        raise InventoryError(
            f"Root is not a directory: {root}"
        )

    if depth < 0:
        raise InventoryError(
            "depth must be >= 0"
        )

    if file_batch_size <= 0:
        raise InventoryError(
            "file_batch_size must be greater than zero"
        )

    created_at = time.time()

    def get_job_directory(
        relative_file_path: str,
    ) -> str:
        path = Path(relative_file_path)
        directory_parts = path.parts[:-1]

        if depth == 0 or not directory_parts:
            return ROOT_DIRECTORY

        return Path(*directory_parts[:depth]).as_posix()

    file_rows: list[tuple] = []
    directory_ids: dict[str, int] = {}
    directory_count = 0
    file_count = 0
    total_bytes = 0

    def add_job_directory(relative_path: str) -> int:
        nonlocal directory_count

        directory_id = directory_ids.get(relative_path)

        if directory_id is not None:
            return directory_id

        directory_id = insert_directory(
            conn,
            (
                run_id,
                relative_path,
                directory_depth(relative_path),
                DIRECTORY_PENDING,
                created_at,
            ),
        )

        directory_ids[relative_path] = directory_id
        directory_count += 1

        return directory_id

    def flush_file_rows() -> None:
        if not file_rows:
            return

        insert_files(
            conn,
            file_rows,
            commit=False,
        )
        file_rows.clear()

    conn.execute("BEGIN")

    try:
        root_directory_id = add_job_directory(
            ROOT_DIRECTORY
        )

        for current_root, dirs, files in os.walk(root):
            current_path = Path(current_root)

            # Sorting makes inventory records deterministic.
            dirs[:] = sorted(
                dirname
                for dirname in dirs
                if dirname not in SKIP_DIRS
            )

            for filename in sorted(files):
                if filename.endswith(TEMP_SUFFIX):
                    continue

                path = current_path / filename

                try:
                    if path.is_symlink():
                        continue

                    stat_result = path.stat(
                        follow_symlinks=False
                    )

                except OSError as exc:
                    raise InventoryError(
                        f"Unable to stat file {path}: {exc}"
                    ) from exc

                relative_file = path.relative_to(root).as_posix()
                job_directory = get_job_directory(relative_file)

                if job_directory == ROOT_DIRECTORY:
                    directory_id = root_directory_id
                else:
                    directory_id = add_job_directory(job_directory)

                file_rows.append(
                    (
                        run_id,
                        directory_id,
                        relative_file,
                        stat_result.st_size,
                        stat_result.st_mtime_ns,
                        FILE_PENDING,
                        created_at,
                    )
                )

                file_count += 1
                total_bytes += stat_result.st_size

                if len(file_rows) >= file_batch_size:
                    flush_file_rows()

        flush_file_rows()
        conn.commit()

    except Exception:
        conn.rollback()
        raise

    return {
        "directories": directory_count,
        "files": file_count,
        "total_bytes": total_bytes,
    }
