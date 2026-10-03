from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import refresh_db
from refresh_logging import event
from inventory import build_inventory
from refresh_controller import run_directories


class RefreshAppError(RuntimeError):
    """Raised for application-level refresh errors."""


def create_new_run(
    db_path: Path,
    root: Path,
    depth: int,
    description: str | None = None,
) -> int:

    root = root.resolve()

    if not root.exists():
        raise RefreshAppError(
            f"Root does not exist: {root}"
        )

    if not root.is_dir():
        raise RefreshAppError(
            f"Root is not a directory: {root}"
        )

    if depth < 0:
        raise RefreshAppError(
            "depth must be >= 0"
        )

    db_path = db_path.resolve()

    db_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = refresh_db.connect(db_path)

    try:

        refresh_db.initialize_database(conn)

        run_id = refresh_db.create_run(
            conn=conn,
            root_path=root,
            started_at=time.time(),
            status="CREATING",
            description=description,
        )

        try:

            inventory = build_inventory(
                conn=conn,
                run_id=run_id,
                root=root,
                depth=depth,
            )

            refresh_db.update_run_status(
                conn,
                run_id,
                "READY",
            )
            event(
                "RUN INVENTORIED",
                run=run_id,
                files=inventory["files"],
                jobs=inventory["directories"],
                bytes=inventory["total_bytes"],
            )

            print()
            print("Refresh run created.")
            print(f"Run ID: {run_id}")
            print(f"Root: {root}")
            print(f"Depth: {depth}")
            print(
                f"Jobs: {inventory['directories']}"
            )
            print(
                f"Files: {inventory['files']}"
            )
            print(
                f"Bytes: {inventory['total_bytes']}"
            )
            print()

            return run_id

        except Exception:

            refresh_db.update_run_status(
                conn,
                run_id,
                "FAILED",
                completed_at=time.time(),
            )

            raise

    finally:

        conn.close()


def resume_run(
    db_path: Path,
    run_id: int,
    *,
    max_jobs: int | None = None,
    max_files: int | None = None,
    max_gb: float | None = None,
    max_hours: float | None = None,
    checkpoint_files: int = 50,
    checkpoint_seconds: float = 60.0,
) -> Any:

    if max_jobs is not None and max_jobs <= 0:
        raise RefreshAppError(
            "--jobs must be greater than zero"
        )

    if max_files is not None and max_files <= 0:
        raise RefreshAppError(
            "--max-files must be greater than zero"
        )

    if max_gb is not None and max_gb <= 0:
        raise RefreshAppError(
            "--max-gb must be greater than zero"
        )

    if max_hours is not None and max_hours <= 0:
        raise RefreshAppError(
            "--max-hours must be greater than zero"
        )

    if checkpoint_files <= 0:
        raise RefreshAppError(
            "--checkpoint-files must be greater than zero"
        )

    if checkpoint_seconds <= 0:
        raise RefreshAppError(
            "--checkpoint-seconds must be greater than zero"
        )

    conn = refresh_db.connect(db_path)

    try:

        refresh_db.initialize_database(conn)

        run = refresh_db.get_run(
            conn,
            run_id,
        )

        if run is None:
            raise RefreshAppError(
                f"Run {run_id} does not exist."
            )

        root = Path(
            str(run["root_path"])
        ).resolve()

        if not root.exists():
            raise RefreshAppError(
                f"Run root no longer exists: {root}"
            )

        # ----------------------------------------------------
        # IMPORTANT:
        #
        # Recover directories that were left IN_PROGRESS.
        # ----------------------------------------------------

        reset_directories = (
            refresh_db.reset_in_progress_directories(
                conn,
                run_id,
            )
        )

        if reset_directories:
            print(
                f"Recovered "
                f"{reset_directories} interrupted "
                f"directory job(s)."
            )

        # ----------------------------------------------------
        # Convert limits.
        # ----------------------------------------------------

        max_bytes = None

        if max_gb is not None:
            max_bytes = int(
                max_gb * 1024 * 1024 * 1024
            )

        max_seconds = None

        if max_hours is not None:
            max_seconds = (
                max_hours * 60 * 60
            )

        # ----------------------------------------------------
        # Mark run as running.
        # ----------------------------------------------------

        refresh_db.update_run_status(
            conn,
            run_id,
            "RUNNING",
        )
        event("RUN RESUMED", run=run_id)

        try:

            result = run_directories(
                conn=conn,
                root=root,
                run_id=run_id,
                max_directories=max_jobs,
                max_files=max_files,
                max_bytes=max_bytes,
                max_seconds=max_seconds,
                checkpoint_files=checkpoint_files,
                checkpoint_seconds=checkpoint_seconds,
            )

        except KeyboardInterrupt:

            refresh_db.update_run_status(
                conn,
                run_id,
                "PAUSED",
            )

            raise

        # ----------------------------------------------------
        # Determine final run state.
        # ----------------------------------------------------

        counts = refresh_db.get_run_file_counts(
            conn,
            run_id,
        )

        if (
            counts["PENDING"] == 0
            and counts["IN_PROGRESS"] == 0
            and counts["FAILED"] == 0
        ):

            refresh_db.update_run_status(
                conn,
                run_id,
                "COMPLETED",
                completed_at=time.time(),
            )
            event("RUN COMPLETED", run=run_id)

        else:

            refresh_db.update_run_status(
                conn,
                run_id,
                "PAUSED",
            )
            event("RUN PAUSED", run=run_id)

        return result

    finally:

        conn.close()


def retry_run(
    db_path: Path,
    run_id: int,
) -> int:

    conn = refresh_db.connect(db_path)

    try:

        refresh_db.initialize_database(conn)

        run = refresh_db.get_run(
            conn,
            run_id,
        )

        if run is None:
            raise RefreshAppError(
                f"Run {run_id} does not exist."
            )

        files_reset = (
            refresh_db.reset_failed_files_to_pending(
                conn,
                run_id,
            )
        )

        directories_reset = (
            refresh_db.reset_failed_directories_to_pending(
                conn,
                run_id,
            )
        )

        # A run that has retryable failures is not completed.
        refresh_db.update_run_status(
            conn,
            run_id,
            "READY",
        )

        event(
            "RETRY RESET",
            run=run_id,
            files=files_reset,
            jobs=directories_reset,
        )

        print(
            f"Reset {files_reset} failed file(s)."
        )

        print(
            f"Reset {directories_reset} failed "
            f"directory job(s)."
        )

        return files_reset

    finally:
        conn.close()
