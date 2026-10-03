from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import refresh_db
from refresh_logging import event
from refresh_engine import (
    COMPLETED,
    FAILED,
    IN_PROGRESS,
    PENDING,
    refresh_one_file,
)


def refresh_file_record(
    conn,
    root: Path,
    file_row: Any,
) -> bool:
    """
    Refresh exactly one SQLite-tracked file.

    SQLite is responsible for state.
    refresh_engine is responsible for the actual filesystem
    operation.

    Returns:

        True  -> file completed successfully
        False -> file failed
    """

    file_id = int(file_row["file_id"])

    relative_path = str(
        file_row["relative_path"]
    )

    expected_size = int(
        file_row["size_bytes"]
    )

    expected_mtime_ns = int(
        file_row["mtime_ns"]
    )

    current_attempts = int(
        file_row["attempts"]
    )

    attempt = current_attempts + 1

    event(
        "FILE STARTED",
        file=file_id,
        path=relative_path,
        attempt=attempt,
    )

    # --------------------------------------------------------
    # State transition:
    #
    # PENDING -> IN_PROGRESS
    # --------------------------------------------------------

    refresh_db.mark_file_in_progress(
        conn=conn,
        file_id=file_id,
        started_at=time.time(),
        attempts=attempt,
    )

    # --------------------------------------------------------
    # Actual filesystem operation.
    # --------------------------------------------------------

    result = refresh_one_file(
        root=root,
        relative_path=relative_path,
        expected_size=expected_size,
        expected_mtime_ns=expected_mtime_ns,
    )

    # --------------------------------------------------------
    # Success.
    # --------------------------------------------------------

    if result["status"] == COMPLETED:

        verified_hash = result["sha256"]

        if not verified_hash:
            # Defensive check.
            #
            # The engine should never report COMPLETED
            # without a verified hash.

            refresh_db.mark_file_failed(
                conn=conn,
                file_id=file_id,
                error_code="ERROR",
                error_message=(
                    "refresh engine reported COMPLETED "
                    "without a SHA-256"
                ),
            )

            return False

        refresh_db.mark_file_completed(
            conn=conn,
            file_id=file_id,
            sha256=verified_hash,
            completed_at=time.time(),
        )

        event(
            "FILE COMPLETED",
            file=file_id,
            path=relative_path,
            bytes=expected_size,
        )

        return True

    # --------------------------------------------------------
    # Failure.
    # --------------------------------------------------------

    error_code = result["error_code"] or "ERROR"
    error_message = result["error_message"] or "refresh failed"

    refresh_db.mark_file_failed(
        conn=conn,
        file_id=file_id,
        error_code=error_code,
        error_message=error_message,
    )

    event(
        "FILE FAILED",
        file=file_id,
        path=relative_path,
        code=error_code,
        message=error_message,
    )

    return False


def recover_interrupted_file(
    conn,
    root: Path,
    file_row: Any,
) -> bool:
    """
    Recover one file that was IN_PROGRESS when the previous
    process stopped.

    Conservative recovery policy:

        IN_PROGRESS
            ->
        PENDING
            ->
        normal refresh

    We deliberately do NOT infer that the previous refresh
    succeeded.

    Even if os.replace() happened immediately before the
    crash, we cannot know that from the SQLite state alone.

    Therefore the file must go through the normal refresh
    process again.

    Returns:

        True  -> successfully refreshed
        False -> failed
    """

    file_id = int(
        file_row["file_id"]
    )

    relative_path = str(
        file_row["relative_path"]
    )

    print(
        f"Recovering interrupted file: "
        f"{relative_path}"
    )

    # --------------------------------------------------------
    # Conservative recovery.
    #
    # We do NOT mark it COMPLETED.
    #
    # We do NOT trust the previous IN_PROGRESS state.
    # --------------------------------------------------------

    refresh_db.reset_file_to_pending(
        conn,
        file_id,
    )

    # --------------------------------------------------------
    # Reload the row from SQLite.
    #
    # This ensures refresh_file_record() receives the actual
    # current database state.
    # --------------------------------------------------------

    row = refresh_db.get_file(
        conn,
        file_id,
    )

    if row is None:
        raise RuntimeError(
            f"File disappeared from database: "
            f"file_id={file_id}"
        )

    # --------------------------------------------------------
    # Run the normal refresh process.
    # --------------------------------------------------------

    return refresh_file_record(
        conn=conn,
        root=root,
        file_row=row,
    )


def recover_interrupted_files(
    conn,
    root: Path,
    run_id: int,
) -> dict[str, int]:
    """
    Recover every file belonging to run_id that was left
    IN_PROGRESS.

    Each interrupted file is conservatively reset to PENDING
    and passed through the normal refresh process.

    Returns:

        {
            "found": number of interrupted files,
            "completed": number successfully recovered,
            "failed": number that failed recovery,
        }
    """

    interrupted = (
        refresh_db.get_in_progress_files(
            conn,
            run_id,
        )
    )

    summary = {
        "found": len(interrupted),
        "completed": 0,
        "failed": 0,
    }

    if not interrupted:
        return summary

    print()
    print(
        f"Found {len(interrupted)} interrupted file(s)."
    )

    for file_row in interrupted:

        relative_path = str(
            file_row["relative_path"]
        )

        print(
            f"Recovering: {relative_path}"
        )

        try:

            success = recover_interrupted_file(
                conn=conn,
                root=root,
                file_row=file_row,
            )

            if success:
                summary["completed"] += 1
            else:
                summary["failed"] += 1

        except Exception as exc:

            summary["failed"] += 1

            file_id = int(
                file_row["file_id"]
            )

            refresh_db.mark_file_failed(
                conn=conn,
                file_id=file_id,
                error_code="ERROR",
                error_message=(
                    "Recovery failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )

    return summary


def refresh_directory_record(
    conn,
    root: Path,
    directory_row: Any,
    max_files: int | None = None,
    stop_requested=None,
    on_file_processed=None,
) -> dict[str, Any]:
    """
    Process one directory job.

    Only PENDING files are processed.

    FAILED files are deliberately left alone. They require
    an explicit retry operation before they can run again.

    Returns:
        {
            "directory_id": int,
            "directory_path": str,
            "status": str,
            "files_processed": int,
            "files_completed": int,
            "files_failed": int,
            "bytes_processed": int,
            "bytes_completed": int,
            "bytes_failed": int,
            "stopped": bool,
        }
    """

    directory_id = int(
        directory_row["directory_id"]
    )

    directory_path = str(
        directory_row["relative_path"]
    )

    refresh_db.mark_directory_in_progress(
        conn,
        directory_id,
    )

    files_processed = 0
    files_completed = 0
    files_failed = 0

    bytes_processed = 0
    bytes_completed = 0
    bytes_failed = 0

    stopped = False

    # --------------------------------------------------------
    # IMPORTANT:
    #
    # Only PENDING files are returned here.
    #
    # FAILED files are NOT automatically retried.
    # --------------------------------------------------------

    files = refresh_db.get_directory_files(
        conn=conn,
        run_id=int(directory_row["run_id"]),
        directory_id=directory_id,
        include_failed=False,
    )

    for file_row in files:

        # ----------------------------------------------------
        # Respect file limit.
        # ----------------------------------------------------

        if (
            max_files is not None
            and files_processed >= max_files
        ):
            break

        # ----------------------------------------------------
        # Do not start another file after Ctrl+C.
        # ----------------------------------------------------

        if (
            stop_requested is not None
            and stop_requested()
        ):
            stopped = True
            break

        relative_path = str(
            file_row["relative_path"]
        )

        file_size = int(
            file_row["size_bytes"]
        )

        print(
            f"Refreshing file: {relative_path}"
        )

        try:

            success = refresh_file_record(
                conn=conn,
                root=root,
                file_row=file_row,
            )

        except KeyboardInterrupt:

            stopped = True
            break

        except Exception as exc:

            files_failed += 1
            bytes_failed += file_size
            bytes_processed += file_size

            error_message = f"{type(exc).__name__}: {exc}"
            refresh_db.mark_file_failed(
                conn=conn,
                file_id=int(file_row["file_id"]),
                error_code="ERROR",
                error_message=error_message,
            )
            event(
                "FILE FAILED",
                file=int(file_row["file_id"]),
                path=relative_path,
                code="ERROR",
                message=error_message,
            )

            if on_file_processed is not None:
                on_file_processed(
                    success=False,
                    size_bytes=file_size,
                )

            continue

        files_processed += 1
        bytes_processed += file_size

        if success:

            files_completed += 1
            bytes_completed += file_size

        else:

            files_failed += 1
            bytes_failed += file_size

        if on_file_processed is not None:
            on_file_processed(
                success=success,
                size_bytes=file_size,
            )

    # --------------------------------------------------------
    # Re-read actual database state.
    #
    # Never infer directory completion only from the loop.
    # --------------------------------------------------------

    counts = refresh_db.get_directory_file_counts(
        conn,
        directory_id,
    )

    if counts["PENDING"] > 0 or counts["IN_PROGRESS"] > 0:
        # Keep the job runnable. Failed sibling files remain failed
        # and are not retried automatically.
        refresh_db.reset_directory_to_pending(conn, directory_id)
        directory_status = "PENDING"

    elif counts["FAILED"] > 0:
        # All files are terminal; this job has failures.
        refresh_db.update_directory_status(
            conn, directory_id, "FAILED"
        )
        directory_status = "FAILED"

    else:
        refresh_db.mark_directory_completed(
            conn, directory_id, time.time()
        )
        directory_status = "COMPLETED"

    return {
        "directory_id": directory_id,
        "directory_path": directory_path,
        "status": directory_status,
        "files_processed": files_processed,
        "files_completed": files_completed,
        "files_failed": files_failed,
        "bytes_processed": bytes_processed,
        "bytes_completed": bytes_completed,
        "bytes_failed": bytes_failed,
        "stopped": stopped,
    }
