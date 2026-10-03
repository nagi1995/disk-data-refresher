from __future__ import annotations

import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import refresh_db
from refresh_logging import event
from refresh_runner import (
    refresh_directory_record,
)


# ============================================================
# Controller result
# ============================================================


@dataclass
class ControllerResult:

    directories_processed: int = 0
    directories_completed: int = 0
    directories_failed: int = 0

    files_processed: int = 0
    files_completed: int = 0
    files_failed: int = 0

    bytes_processed: int = 0
    bytes_completed: int = 0
    bytes_failed: int = 0

    stopped_by_user: bool = False
    stopped_by_file_limit: bool = False
    stopped_by_byte_limit: bool = False
    stopped_by_time_limit: bool = False

    @property
    def success(self) -> bool:
        return (
            self.directories_failed == 0
            and self.files_failed == 0
            and not self.stopped_by_user
            and not self.stopped_by_byte_limit
            and not self.stopped_by_file_limit
            and not self.stopped_by_time_limit
        )



# ============================================================
# Graceful stop handling
# ============================================================


class StopController:
    """
    Handle Ctrl+C without interrupting the filesystem operation
    halfway through a file.

    First Ctrl+C:
        request graceful stop.

    Second Ctrl+C:
        allow the normal KeyboardInterrupt to propagate.
    """

    def __init__(self) -> None:

        self.requested = False
        self._previous_handler = None

    def install(self) -> None:

        self._previous_handler = signal.getsignal(
            signal.SIGINT
        )

        signal.signal(
            signal.SIGINT,
            self._handle_sigint,
        )

    def uninstall(self) -> None:

        if self._previous_handler is not None:

            signal.signal(
                signal.SIGINT,
                self._previous_handler,
            )

    def _handle_sigint(
        self,
        signum,
        frame,
    ) -> None:

        if not self.requested:

            self.requested = True

            print()
            print(
                "Ctrl+C received. "
                "Finishing the current file, "
                "then stopping..."
            )

        else:

            print()
            print(
                "Second Ctrl+C received. "
                "Stopping immediately."
            )

            raise KeyboardInterrupt

    def is_requested(self) -> bool:

        return self.requested


# ============================================================
# Time / limit helpers
# ============================================================


def time_limit_reached(
    started_monotonic: float,
    max_seconds: float | None,
) -> bool:

    if max_seconds is None:
        return False

    return (
        time.monotonic()
        - started_monotonic
        >= max_seconds
    )


# ============================================================
# Application checkpoint manager
# ============================================================


class CheckpointManager:
    def __init__(
        self,
        conn,
        run_id: int,
        *,
        checkpoint_files: int = 50,
        checkpoint_seconds: float = 60.0,
    ) -> None:
        if checkpoint_files <= 0:
            raise ValueError("checkpoint_files must be greater than zero")
        if checkpoint_seconds <= 0:
            raise ValueError("checkpoint_seconds must be greater than zero")

        self.conn = conn
        self.run_id = run_id
        self.checkpoint_files = checkpoint_files
        self.checkpoint_seconds = checkpoint_seconds
        self.files_since_checkpoint = 0
        self.last_checkpoint_monotonic = time.monotonic()

    def note_file(self, *, success: bool, size_bytes: int) -> None:
        self.files_since_checkpoint += 1
        now = time.monotonic()
        due_to_files = self.files_since_checkpoint >= self.checkpoint_files
        due_to_time = (
            now - self.last_checkpoint_monotonic
            >= self.checkpoint_seconds
        )
        if due_to_files or due_to_time:
            reasons = []
            if due_to_files:
                reasons.append("file-count")
            if due_to_time:
                reasons.append("time")
            self.checkpoint("+".join(reasons))

    def checkpoint(self, reason: str) -> dict:
        snapshot = refresh_db.checkpoint_run(
            self.conn,
            self.run_id,
            reason=reason,
        )
        self.files_since_checkpoint = 0
        self.last_checkpoint_monotonic = time.monotonic()
        event(
            "CHECKPOINT",
            run=self.run_id,
            reason=reason,
            files=snapshot["completed_files"],
            bytes=snapshot["completed_bytes"],
            pending=snapshot["pending_files"],
            failed=snapshot["failed_files"],
        )
        return snapshot


# ============================================================
# Run controller
# ============================================================

def run_directories(
    conn,
    root: Path,
    run_id: int,
    *,
    max_directories: int | None = None,
    max_files: int | None = None,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
    checkpoint_files: int = 50,
    checkpoint_seconds: float = 60.0,
) -> ControllerResult:

    """
    Process directory jobs belonging to one refresh run.

    Limits:

        max_directories
        max_files
        max_bytes
        max_seconds

    Ctrl+C performs a graceful stop.
    """

    result = ControllerResult()

    root = root.resolve()

    started_monotonic = time.monotonic()

    stop_controller = StopController()

    checkpoint_manager = CheckpointManager(
        conn,
        run_id,
        checkpoint_files=checkpoint_files,
        checkpoint_seconds=checkpoint_seconds,
    )

    stop_controller.install()
    event("RUN STARTED", run=run_id)

    try:

        # ----------------------------------------------------
        # Recover files interrupted by a previous invocation.
        # ----------------------------------------------------

        recovery = refresh_runner_recovery(
            conn=conn,
            root=root,
            run_id=run_id,
        )

        result.files_completed += recovery["completed"]
        result.files_failed += recovery["failed"]

        event(
            "RECOVERY",
            run=run_id,
            completed=recovery["completed"],
            failed=recovery["failed"],
        )

        if recovery["failed"] > 0:
            print(
                f"Recovery completed with "
                f"{recovery['failed']} failure(s)."
            )

        # ----------------------------------------------------
        # Main directory loop.
        # ----------------------------------------------------

        directories_processed = 0

        while True:

            # ------------------------------------------------
            # Ctrl+C
            # ------------------------------------------------

            if stop_controller.is_requested():

                result.stopped_by_user = True
                break

            # ------------------------------------------------
            # Directory limit
            # ------------------------------------------------

            if (
                max_directories is not None
                and directories_processed
                >= max_directories
            ):

                break

            # ------------------------------------------------
            # File limit
            # ------------------------------------------------

            if (
                max_files is not None
                and result.files_processed
                >= max_files
            ):

                result.stopped_by_file_limit = True
                break

            # ------------------------------------------------
            # Byte limit
            # ------------------------------------------------

            if (
                max_bytes is not None
                and result.bytes_processed
                >= max_bytes
            ):

                result.stopped_by_byte_limit = True
                break

            # ------------------------------------------------
            # Time limit
            # ------------------------------------------------

            if time_limit_reached(
                started_monotonic,
                max_seconds,
            ):

                result.stopped_by_time_limit = True
                break

            # ------------------------------------------------
            # Get next directory.
            # ------------------------------------------------

            directories = (
                refresh_db.get_pending_directories(
                    conn,
                    run_id,
                )
            )

            if not directories:
                break

            directory_row = directories[0]

            directory_id = int(
                directory_row["directory_id"]
            )

            directory_path = str(
                directory_row["relative_path"]
            )

            print()
            print("=" * 72)
            print(
                f"Directory job: {directory_id}"
            )
            event(
                "JOB STARTED",
                run=run_id,
                job=directory_id,
                path=directory_path,
            )

            print(
                f"Path: {directory_path}"
            )
            print("=" * 72)

            # ------------------------------------------------
            # Remaining file budget.
            # ------------------------------------------------

            directory_file_limit = None

            if max_files is not None:

                remaining_files = (
                    max_files
                    - result.files_processed
                )

                if remaining_files <= 0:

                    result.stopped_by_file_limit = True
                    break

                directory_file_limit = remaining_files

            # ------------------------------------------------
            # Process directory.
            # ------------------------------------------------

            directory_result = (
                refresh_directory_record(
                    conn=conn,
                    root=root,
                    directory_row=directory_row,
                    max_files=directory_file_limit,
                    stop_requested=(
                        stop_controller.is_requested
                    ),
                    on_file_processed=(
                        checkpoint_manager.note_file
                    ),
                )
            )

            directories_processed += 1

            result.directories_processed += 1

            result.files_processed += int(
                directory_result["files_processed"]
            )

            result.files_completed += int(
                directory_result["files_completed"]
            )

            result.files_failed += int(
                directory_result["files_failed"]
            )

            result.bytes_processed += int(
                directory_result["bytes_processed"]
            )

            result.bytes_completed += int(
                directory_result["bytes_completed"]
            )

            result.bytes_failed += int(
                directory_result["bytes_failed"]
            )

            directory_status = (
                directory_result["status"]
            )

            if directory_status == "COMPLETED":

                result.directories_completed += 1

            elif directory_status == "FAILED":

                result.directories_failed += 1

            event(
                "JOB COMPLETED" if directory_status == "COMPLETED" else "JOB FAILED",
                run=run_id,
                job=directory_id,
                path=directory_path,
                files=directory_result["files_processed"],
                failed=directory_result["files_failed"],
            )

            # ------------------------------------------------
            # User requested stop.
            # ------------------------------------------------

            if directory_result["stopped"]:

                result.stopped_by_user = (
                    stop_controller.is_requested()
                )

                break

            # ------------------------------------------------
            # File limit.
            # ------------------------------------------------

            if (
                max_files is not None
                and result.files_processed
                >= max_files
            ):

                result.stopped_by_file_limit = True
                break

            # ------------------------------------------------
            # Byte limit.
            # ------------------------------------------------

            if (
                max_bytes is not None
                and result.bytes_processed
                >= max_bytes
            ):

                result.stopped_by_byte_limit = True
                break

            # ------------------------------------------------
            # Time limit.
            # ------------------------------------------------

            if time_limit_reached(
                started_monotonic,
                max_seconds,
            ):

                result.stopped_by_time_limit = True
                break

        checkpoint_manager.checkpoint("invocation-end")

        event(
            "RUN PAUSED" if (
                result.stopped_by_user
                or result.stopped_by_file_limit
                or result.stopped_by_byte_limit
                or result.stopped_by_time_limit
            ) else "RUN PROCESSING COMPLETE",
            run=run_id,
        )

    finally:

        stop_controller.uninstall()

    return result


# ============================================================
# Recovery adapter
# ============================================================


def refresh_runner_recovery(
    conn,
    root: Path,
    run_id: int,
) -> dict[str, int]:
    """
    Small adapter around the recovery implementation in
    refresh_runner.py.

    Kept here so refresh_controller.py does not duplicate
    recovery logic.
    """

    from refresh_runner import (
        recover_interrupted_files,
    )

    return recover_interrupted_files(
        conn=conn,
        root=root,
        run_id=run_id,
    )

