from __future__ import annotations

import argparse
from pathlib import Path

import refresh_db
import refresh_app
from refresh_logging import configure_logging, DEFAULT_LOG_DIR, DEFAULT_LOG_FILE


DEFAULT_DB = (
    Path.home()
    / ".refresh"
    / "refresh.db"
)


def format_bytes(value: int) -> str:

    value = float(value)

    units = [
        "B",
        "KB",
        "MB",
        "GB",
        "TB",
    ]

    for unit in units:

        if value < 1024:
            return f"{value:.2f} {unit}"

        value /= 1024

    return f"{value:.2f} PB"


def cmd_start(args) -> int:

    run_id = refresh_app.create_new_run(
        db_path=args.db,
        root=args.root,
        depth=args.depth,
        description=args.description,
    )

    print(
        f"Created run {run_id}."
    )

    return 0


def cmd_resume(args) -> int:

    result = refresh_app.resume_run(
        db_path=args.db,
        run_id=args.run_id,
        max_jobs=args.jobs,
        max_files=args.max_files,
        max_gb=args.max_gb,
        max_hours=args.max_hours,
        checkpoint_files=args.checkpoint_files,
        checkpoint_seconds=args.checkpoint_seconds,
    )

    print()
    print("Resume complete.")
    print(
        f"Directories processed: "
        f"{result.directories_processed}"
    )
    print(
        f"Directories completed: "
        f"{result.directories_completed}"
    )
    print(
        f"Directories failed: "
        f"{result.directories_failed}"
    )
    print(
        f"Files processed: "
        f"{result.files_processed}"
    )
    print(
        f"Files completed: "
        f"{result.files_completed}"
    )
    print(
        f"Files failed: "
        f"{result.files_failed}"
    )
    print(
        f"Bytes processed: "
        f"{format_bytes(result.bytes_processed)}"
    )

    if result.stopped_by_user:
        print("Stopped by Ctrl+C.")

    if result.stopped_by_file_limit:
        print("Stopped by --max-files.")

    if result.stopped_by_byte_limit:
        print("Stopped by --max-gb.")

    if result.stopped_by_time_limit:
        print("Stopped by --max-hours.")

    return 0


def cmd_status(args) -> int:

    conn = refresh_db.connect(args.db)

    try:

        refresh_db.initialize_database(conn)

        run = refresh_db.get_run(
            conn,
            args.run_id,
        )

        if run is None:
            print(
                f"Run {args.run_id} not found."
            )
            return 1

        file_counts = (
            refresh_db.get_run_file_counts(
                conn,
                args.run_id,
            )
        )

        directory_counts = (
            refresh_db.get_run_directory_counts(
                conn,
                args.run_id,
            )
        )

        print(
            f"Run ID: {run['run_id']}"
        )
        print(
            f"Root: {run['root_path']}"
        )
        print(
            f"Status: {run['status']}"
        )
        print()
        print("Files:")
        print(
            f"  PENDING: "
            f"{file_counts['PENDING']}"
        )
        print(
            f"  IN_PROGRESS: "
            f"{file_counts['IN_PROGRESS']}"
        )
        print(
            f"  COMPLETED: "
            f"{file_counts['COMPLETED']}"
        )
        print(
            f"  FAILED: "
            f"{file_counts['FAILED']}"
        )
        print()
        print("Jobs:")
        print(
            f"  PENDING: "
            f"{directory_counts['PENDING']}"
        )
        print(
            f"  IN_PROGRESS: "
            f"{directory_counts['IN_PROGRESS']}"
        )
        print(
            f"  COMPLETED: "
            f"{directory_counts['COMPLETED']}"
        )
        print(
            f"  FAILED: "
            f"{directory_counts['FAILED']}"
        )

        return 0

    finally:

        conn.close()


def cmd_jobs(args) -> int:

    conn = refresh_db.connect(args.db)

    try:

        refresh_db.initialize_database(conn)

        rows = conn.execute(
            """
            SELECT
                d.directory_id,
                d.relative_path,
                d.depth,
                d.status,
                COUNT(f.file_id) AS file_count,
                COALESCE(
                    SUM(f.size_bytes),
                    0
                ) AS total_bytes
            FROM directories d
            LEFT JOIN files f
                ON f.directory_id = d.directory_id
            WHERE d.run_id = ?
            GROUP BY
                d.directory_id,
                d.relative_path,
                d.depth,
                d.status
            ORDER BY
                d.depth,
                d.directory_id
            """,
            (args.run_id,),
        ).fetchall()

        for row in rows:

            print(
                f"{row['directory_id']:5d}  "
                f"{row['status']:12s}  "
                f"{row['file_count']:6d} files  "
                f"{format_bytes(row['total_bytes']):>12s}  "
                f"{row['relative_path']}"
            )

        return 0

    finally:

        conn.close()


def cmd_failures(args) -> int:

    conn = refresh_db.connect(args.db)

    try:

        refresh_db.initialize_database(conn)

        rows = refresh_db.get_failure_history(
            conn,
            args.run_id,
        )

        if not rows:
            print("No failures.")
            return 0

        for row in rows:

            print()
            print(
                f"Failure ID: "
                f"{row['failure_id']}"
            )
            print(
                f"File: "
                f"{row['relative_path']}"
            )
            print(
                f"Attempt: "
                f"{row['attempt_number']}"
            )
            print(
                f"Code: "
                f"{row['error_code']}"
            )
            print(
                f"Message: "
                f"{row['error_message']}"
            )

        return 0

    finally:

        conn.close()


def cmd_retry(args) -> int:

    count = refresh_app.retry_run(
        db_path=args.db,
        run_id=args.run_id,
    )

    print(
        f"{count} file(s) are ready for retry."
    )

    return 0


def cmd_history(args) -> int:

    conn = refresh_db.connect(args.db)

    try:

        refresh_db.initialize_database(conn)

        rows = conn.execute(
            """
            SELECT
                run_id,
                root_path,
                status,
                started_at,
                completed_at,
                description
            FROM refresh_runs
            ORDER BY run_id DESC
            """
        ).fetchall()

        for row in rows:

            print(
                f"{row['run_id']:5d}  "
                f"{row['status']:12s}  "
                f"{row['root_path']}"
            )

        return 0

    finally:

        conn.close()


def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        description=(
            "Safe, resumable HDD refresh utility"
        )
    )

    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=(
            "SQLite database path "
            "(default: ~/.refresh/refresh.db)"
        ),
    )

    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_DIR / DEFAULT_LOG_FILE,
        help="Human-readable event log path.",
    )

    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    # --------------------------------------------------------
    # start
    # --------------------------------------------------------

    start = subparsers.add_parser(
        "start",
        help="Create a new refresh run.",
    )

    start.add_argument(
        "root",
        type=Path,
    )

    start.add_argument(
        "--depth",
        type=int,
        required=True,
    )

    start.add_argument(
        "--description",
        default=None,
    )

    start.set_defaults(
        func=cmd_start
    )

    # --------------------------------------------------------
    # resume
    # --------------------------------------------------------

    resume = subparsers.add_parser(
        "resume",
        help="Resume a refresh run.",
    )

    resume.add_argument(
        "run_id",
        type=int,
    )

    resume.add_argument(
        "--jobs",
        type=int,
        default=None,
    )

    resume.add_argument(
        "--max-files",
        type=int,
        default=None,
    )

    resume.add_argument(
        "--max-gb",
        type=float,
        default=None,
    )

    resume.add_argument(
        "--max-hours",
        type=float,
        default=None,
    )

    resume.add_argument(
        "--checkpoint-files",
        type=int,
        default=50,
        help="Checkpoint after this many processed files.",
    )

    resume.add_argument(
        "--checkpoint-seconds",
        type=float,
        default=60.0,
        help="Checkpoint after this many seconds.",
    )

    resume.set_defaults(
        func=cmd_resume
    )

    # --------------------------------------------------------
    # status
    # --------------------------------------------------------

    status = subparsers.add_parser(
        "status",
        help="Show run status.",
    )

    status.add_argument(
        "run_id",
        type=int,
    )

    status.set_defaults(
        func=cmd_status
    )

    # --------------------------------------------------------
    # jobs
    # --------------------------------------------------------

    jobs = subparsers.add_parser(
        "jobs",
        help="List directory jobs.",
    )

    jobs.add_argument(
        "run_id",
        type=int,
    )

    jobs.set_defaults(
        func=cmd_jobs
    )

    # --------------------------------------------------------
    # failures
    # --------------------------------------------------------

    failures = subparsers.add_parser(
        "failures",
        help="Show failure history.",
    )

    failures.add_argument(
        "run_id",
        type=int,
    )

    failures.set_defaults(
        func=cmd_failures
    )

    # --------------------------------------------------------
    # retry
    # --------------------------------------------------------

    retry = subparsers.add_parser(
        "retry",
        help="Retry failed files.",
    )

    retry.add_argument(
        "run_id",
        type=int,
    )

    retry.set_defaults(
        func=cmd_retry
    )

    # --------------------------------------------------------
    # history
    # --------------------------------------------------------

    history = subparsers.add_parser(
        "history",
        help="List previous runs.",
    )

    history.set_defaults(
        func=cmd_history
    )

    return parser


def main() -> int:

    parser = build_parser()

    args = parser.parse_args()

    configure_logging(
        args.log,
        verbose=args.verbose,
    )

    try:

        return args.func(args)

    except refresh_app.RefreshAppError as exc:

        print(
            f"ERROR: {exc}"
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )

