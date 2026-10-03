from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Any
import time


SCHEMA_VERSION = 3


def connect(db_path: Path) -> sqlite3.Connection:
    """
    Open the SQLite database and configure it for this application.

    WAL is used because the database contains job state that is
    frequently updated while the refresh process is running.
    """

    db_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    conn = sqlite3.connect(
        db_path,
        timeout=30,
    )

    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    return conn


def initialize_database(
    conn: sqlite3.Connection,
) -> None:

    # --------------------------------------------------------
    # Create base tables.
    #
    # SQLite CREATE TABLE IF NOT EXISTS will preserve an
    # existing database.
    # --------------------------------------------------------

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS refresh_runs (
            run_id              INTEGER PRIMARY KEY AUTOINCREMENT,

            root_path           TEXT NOT NULL,

            started_at           REAL,
            completed_at        REAL,

            status              TEXT NOT NULL,

            description         TEXT,

            last_checkpoint_at  REAL,
            checkpoint_count    INTEGER NOT NULL DEFAULT 0,
            checkpoint_files    INTEGER NOT NULL DEFAULT 0,
            checkpoint_bytes    INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS directories (
            directory_id        INTEGER PRIMARY KEY AUTOINCREMENT,

            run_id              INTEGER NOT NULL,

            relative_path       TEXT NOT NULL,

            depth               INTEGER NOT NULL,

            status              TEXT NOT NULL,

            created_at          REAL NOT NULL,

            completed_at        REAL,

            FOREIGN KEY(run_id)
                REFERENCES refresh_runs(run_id)
                ON DELETE CASCADE,

            UNIQUE(
                run_id,
                relative_path
            )
        );

        CREATE TABLE IF NOT EXISTS files (
            file_id             INTEGER PRIMARY KEY AUTOINCREMENT,

            run_id              INTEGER NOT NULL,

            directory_id        INTEGER,

            relative_path       TEXT NOT NULL,

            size_bytes          INTEGER NOT NULL,

            mtime_ns            INTEGER NOT NULL,

            sha256              TEXT,

            status              TEXT NOT NULL,

            attempts            INTEGER NOT NULL DEFAULT 0,

            last_error_code     TEXT,

            last_error_message  TEXT,

            created_at          REAL NOT NULL,

            started_at          REAL,

            completed_at        REAL,

            FOREIGN KEY(run_id)
                REFERENCES refresh_runs(run_id)
                ON DELETE CASCADE,

            FOREIGN KEY(directory_id)
                REFERENCES directories(directory_id)
                ON DELETE SET NULL,

            UNIQUE(
                run_id,
                relative_path
            )
        );

        CREATE INDEX IF NOT EXISTS idx_directories_run_status
        ON directories(
            run_id,
            status
        );

        CREATE INDEX IF NOT EXISTS idx_directories_run_depth
        ON directories(
            run_id,
            depth
        );

        CREATE INDEX IF NOT EXISTS idx_files_run_status
        ON files(
            run_id,
            status
        );

        CREATE INDEX IF NOT EXISTS idx_files_directory
        ON files(
            directory_id
        );

        CREATE INDEX IF NOT EXISTS idx_files_run_directory_status
        ON files(
            run_id,
            directory_id,
            status
        );

        CREATE TABLE IF NOT EXISTS failures (
            failure_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL,
            file_id         INTEGER NOT NULL,
            attempt_number  INTEGER NOT NULL,
            error_code      TEXT NOT NULL,
            error_message   TEXT NOT NULL,
            created_at      REAL NOT NULL,

            FOREIGN KEY(run_id)
                REFERENCES refresh_runs(run_id)
                ON DELETE CASCADE,

            FOREIGN KEY(file_id)
                REFERENCES files(file_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_failures_run
        ON failures(run_id);

        CREATE INDEX IF NOT EXISTS idx_failures_file
        ON failures(file_id);

        """
    )

    # --------------------------------------------------------
    # Phase 7 migration for checkpoint metadata.
    # --------------------------------------------------------

    run_columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(refresh_runs)"
        ).fetchall()
    }

    for column, definition in (
        ("last_checkpoint_at", "REAL"),
        ("checkpoint_count", "INTEGER NOT NULL DEFAULT 0"),
        ("checkpoint_files", "INTEGER NOT NULL DEFAULT 0"),
        ("checkpoint_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in run_columns:
            conn.execute(
                f"ALTER TABLE refresh_runs ADD COLUMN {column} {definition}"
            )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_checkpoints (
            checkpoint_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id              INTEGER NOT NULL,
            created_at          REAL NOT NULL,
            reason              TEXT NOT NULL,
            total_files         INTEGER NOT NULL,
            pending_files       INTEGER NOT NULL,
            in_progress_files   INTEGER NOT NULL,
            completed_files     INTEGER NOT NULL,
            failed_files        INTEGER NOT NULL,
            total_bytes         INTEGER NOT NULL,
            completed_bytes     INTEGER NOT NULL,
            failed_bytes        INTEGER NOT NULL,
            pending_directories INTEGER NOT NULL,
            in_progress_directories INTEGER NOT NULL,
            completed_directories INTEGER NOT NULL,
            failed_directories  INTEGER NOT NULL,
            FOREIGN KEY(run_id) REFERENCES refresh_runs(run_id) ON DELETE CASCADE
        );
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_run_checkpoints_run_created
        ON run_checkpoints(run_id, created_at);
        """
    )

    # --------------------------------------------------------
    # Migration:
    #
    # Existing Phase 1/2 databases do not have sha256.
    #
    # SQLite does not support:
    #
    # ALTER TABLE ... ADD COLUMN IF NOT EXISTS
    #
    # so inspect PRAGMA table_info().
    # --------------------------------------------------------

    columns = {
        row["name"]
        for row in conn.execute(
            "PRAGMA table_info(files)"
        ).fetchall()
    }

    if "sha256" not in columns:

        conn.execute(
            """
            ALTER TABLE files
            ADD COLUMN sha256 TEXT
            """
        )

    # --------------------------------------------------------
    # Schema version.
    # --------------------------------------------------------

    conn.execute(
        """
        INSERT INTO metadata(key, value)
        VALUES('schema_version', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (str(SCHEMA_VERSION),),
    )

    conn.commit()

def create_run(
    conn: sqlite3.Connection,
    root_path: Path,
    started_at: float,
    status: str = "CREATED",
    description: str | None = None,
) -> int:

    cursor = conn.execute(
        """
        INSERT INTO refresh_runs(
            root_path,
            started_at,
            status,
            description
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            str(root_path.resolve()),
            started_at,
            status,
            description,
        ),
    )

    conn.commit()

    return int(cursor.lastrowid)


def get_run(
    conn: sqlite3.Connection,
    run_id: int,
) -> sqlite3.Row | None:

    return conn.execute(
        """
        SELECT *
        FROM refresh_runs
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()


def update_run_status(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    completed_at: float | None = None,
) -> None:

    conn.execute(
        """
        UPDATE refresh_runs
        SET
            status = ?,
            completed_at = ?
        WHERE run_id = ?
        """,
        (
            status,
            completed_at,
            run_id,
        ),
    )

    conn.commit()


def insert_directories(
    conn: sqlite3.Connection,
    rows: Iterable[tuple],
    *,
    commit: bool = True,
) -> None:

    conn.executemany(
        """
        INSERT INTO directories(
            run_id,
            relative_path,
            depth,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )

    if commit:
        conn.commit()


def insert_directory(
    conn: sqlite3.Connection,
    row: tuple,
) -> int:
    """Insert one directory job without committing the transaction."""

    cursor = conn.execute(
        """
        INSERT INTO directories(
            run_id,
            relative_path,
            depth,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        row,
    )

    return int(cursor.lastrowid)


def insert_files(
    conn: sqlite3.Connection,
    rows: Iterable[tuple],
    *,
    commit: bool = True,
) -> None:

    conn.executemany(
        """
        INSERT INTO files(
            run_id,
            directory_id,
            relative_path,
            size_bytes,
            mtime_ns,
            status,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    if commit:
        conn.commit()

def get_pending_directories(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT *
        FROM directories
        WHERE run_id = ?
          AND status = 'PENDING'
        ORDER BY depth, directory_id
        """,
        (run_id,),
    ).fetchall()


def get_directory_files(
    conn: sqlite3.Connection,
    run_id: int,
    directory_id: int,
    *,
    include_failed: bool = False,
) -> list[sqlite3.Row]:
    """
    Return files belonging to one job.

    Normal resume processes only PENDING files.

    FAILED files are retried only when explicitly requested.
    """

    if include_failed:
        sql = """
            SELECT *
            FROM files
            WHERE run_id = ?
              AND directory_id = ?
              AND status != 'COMPLETED'
            ORDER BY file_id
        """
    else:
        sql = """
            SELECT *
            FROM files
            WHERE run_id = ?
              AND directory_id = ?
              AND status = 'PENDING'
            ORDER BY file_id
        """

    return conn.execute(
        sql,
        (
            run_id,
            directory_id,
        ),
    ).fetchall()

def reset_failed_files_to_pending(
    conn: sqlite3.Connection,
    run_id: int,
) -> int:
    """
    Explicitly make all failed files in a run eligible for retry.

    Failure information remains in the files table's last-error
    fields until the next attempt, while the attempt counter is
    preserved.
    """

    cursor = conn.execute(
        """
        UPDATE files
        SET
            status = 'PENDING',
            started_at = NULL,
            completed_at = NULL
        WHERE run_id = ?
          AND status = 'FAILED'
        """,
        (run_id,),
    )

    conn.commit()

    return cursor.rowcount

def reset_failed_files_in_directory_to_pending(
    conn: sqlite3.Connection,
    directory_id: int,
) -> int:
    cursor = conn.execute(
        """
        UPDATE files
        SET
            status = 'PENDING',
            started_at = NULL,
            completed_at = NULL
        WHERE directory_id = ?
          AND status = 'FAILED'
        """,
        (directory_id,),
    )

    conn.commit()

    return cursor.rowcount

def reset_failed_directories_to_pending(
    conn: sqlite3.Connection,
    run_id: int,
) -> int:
    """
    Reset FAILED directory jobs to PENDING.

    This is used together with retrying their FAILED files.
    """

    cursor = conn.execute(
        """
        UPDATE directories
        SET
            status = 'PENDING',
            completed_at = NULL
        WHERE run_id = ?
          AND status = 'FAILED'
        """,
        (run_id,),
    )

    conn.commit()

    return cursor.rowcount


def update_file_status(
    conn: sqlite3.Connection,
    file_id: int,
    status: str,
    *,
    attempts: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    started_at: float | None = None,
    completed_at: float | None = None,
) -> None:

    conn.execute(
        """
        UPDATE files
        SET
            status = ?,
            attempts = COALESCE(?, attempts),
            last_error_code = ?,
            last_error_message = ?,
            started_at = COALESCE(?, started_at),
            completed_at = ?
        WHERE file_id = ?
        """,
        (
            status,
            attempts,
            error_code,
            error_message,
            started_at,
            completed_at,
            file_id,
        ),
    )

def get_failure_history(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT
            failures.failure_id,
            failures.file_id,
            files.relative_path,
            failures.attempt_number,
            failures.error_code,
            failures.error_message,
            failures.created_at
        FROM failures
        JOIN files
            ON files.file_id = failures.file_id
        WHERE failures.run_id = ?
        ORDER BY failures.failure_id
        """,
        (run_id,),
    ).fetchall()


def get_all_runs(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT *
        FROM refresh_runs
        ORDER BY run_id DESC
        """
    ).fetchall()

def get_run_summary(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, int]:

    file_counts = get_run_file_counts(
        conn,
        run_id,
    )

    directory_counts = get_run_directory_counts(
        conn,
        run_id,
    )

    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total_files,
            COALESCE(SUM(size_bytes), 0) AS total_bytes,
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'COMPLETED'
                        THEN size_bytes
                        ELSE 0
                    END
                ),
                0
            ) AS completed_bytes,
            COALESCE(
                SUM(
                    CASE
                        WHEN status = 'FAILED'
                        THEN size_bytes
                        ELSE 0
                    END
                ),
                0
            ) AS failed_bytes
        FROM files
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    return {
        "total_files": int(row["total_files"]),
        "total_bytes": int(row["total_bytes"]),
        "completed_bytes": int(row["completed_bytes"]),
        "failed_bytes": int(row["failed_bytes"]),

        "pending_files": file_counts["PENDING"],
        "in_progress_files": file_counts["IN_PROGRESS"],
        "completed_files": file_counts["COMPLETED"],
        "failed_files": file_counts["FAILED"],

        "pending_directories":
            directory_counts["PENDING"],

        "in_progress_directories":
            directory_counts["IN_PROGRESS"],

        "completed_directories":
            directory_counts["COMPLETED"],

        "failed_directories":
            directory_counts["FAILED"],
    }

def reset_in_progress_directories(
    conn: sqlite3.Connection,
    run_id: int,
) -> int:

    cursor = conn.execute(
        """
        UPDATE directories
        SET
            status = 'PENDING',
            completed_at = NULL
        WHERE run_id = ?
          AND status = 'IN_PROGRESS'
        """,
        (run_id,),
    )

    conn.commit()

    return cursor.rowcount



def mark_file_in_progress(
    conn: sqlite3.Connection,
    file_id: int,
    started_at: float,
    attempts: int,
) -> None:

    conn.execute(
        """
        UPDATE files
        SET
            status = 'IN_PROGRESS',
            attempts = ?,
            started_at = ?,
            completed_at = NULL,
            last_error_code = NULL,
            last_error_message = NULL
        WHERE file_id = ?
        """,
        (
            attempts,
            started_at,
            file_id,
        ),
    )

    conn.commit()


def mark_file_completed(
    conn: sqlite3.Connection,
    file_id: int,
    sha256: str,
    completed_at: float,
) -> None:

    conn.execute(
        """
        UPDATE files
        SET
            status = 'COMPLETED',
            sha256 = ?,
            last_error_code = NULL,
            last_error_message = NULL,
            completed_at = ?
        WHERE file_id = ?
        """,
        (
            sha256,
            completed_at,
            file_id,
        ),
    )

    conn.commit()


def mark_file_failed(
    conn: sqlite3.Connection,
    file_id: int,
    error_code: str,
    error_message: str,
) -> None:

    now = time.time()

    row = conn.execute(
        """
        SELECT
            run_id,
            attempts
        FROM files
        WHERE file_id = ?
        """,
        (file_id,),
    ).fetchone()

    if row is None:
        raise RuntimeError(
            f"Unknown file_id: {file_id}"
        )

    run_id = int(row["run_id"])
    attempt_number = int(row["attempts"])

    conn.execute(
        """
        UPDATE files
        SET
            status = 'FAILED',
            last_error_code = ?,
            last_error_message = ?,
            completed_at = NULL
        WHERE file_id = ?
        """,
        (
            error_code,
            error_message,
            file_id,
        ),
    )

    conn.execute(
        """
        INSERT INTO failures(
            run_id,
            file_id,
            attempt_number,
            error_code,
            error_message,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            file_id,
            attempt_number,
            error_code,
            error_message,
            now,
        ),
    )

    conn.commit()



def get_file(
    conn: sqlite3.Connection,
    file_id: int,
) -> sqlite3.Row | None:

    return conn.execute(
        """
        SELECT *
        FROM files
        WHERE file_id = ?
        """,
        (file_id,),
    ).fetchone()


def get_in_progress_files(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT *
        FROM files
        WHERE run_id = ?
          AND status = 'IN_PROGRESS'
        ORDER BY file_id
        """,
        (run_id,),
    ).fetchall()


def reset_file_to_pending(
    conn: sqlite3.Connection,
    file_id: int,
) -> None:

    conn.execute(
        """
        UPDATE files
        SET
            status = 'PENDING',
            last_error_code = NULL,
            last_error_message = NULL,
            started_at = NULL,
            completed_at = NULL
        WHERE file_id = ?
        """,
        (file_id,),
    )

    conn.commit()

def update_directory_status(
    conn: sqlite3.Connection,
    directory_id: int,
    status: str,
    completed_at: float | None = None,
) -> None:

    conn.execute(
        """
        UPDATE directories
        SET
            status = ?,
            completed_at = ?
        WHERE directory_id = ?
        """,
        (
            status,
            completed_at,
            directory_id,
        ),
    )

def get_directory(
    conn: sqlite3.Connection,
    directory_id: int,
) -> sqlite3.Row | None:

    return conn.execute(
        """
        SELECT *
        FROM directories
        WHERE directory_id = ?
        """,
        (directory_id,),
    ).fetchone()


def mark_directory_in_progress(
    conn: sqlite3.Connection,
    directory_id: int,
) -> None:

    conn.execute(
        """
        UPDATE directories
        SET
            status = 'IN_PROGRESS',
            completed_at = NULL
        WHERE directory_id = ?
        """,
        (directory_id,),
    )

    conn.commit()


def mark_directory_completed(
    conn: sqlite3.Connection,
    directory_id: int,
    completed_at: float,
) -> None:

    conn.execute(
        """
        UPDATE directories
        SET
            status = 'COMPLETED',
            completed_at = ?
        WHERE directory_id = ?
        """,
        (
            completed_at,
            directory_id,
        ),
    )

    conn.commit()


def reset_directory_to_pending(
    conn: sqlite3.Connection,
    directory_id: int,
) -> None:

    conn.execute(
        """
        UPDATE directories
        SET
            status = 'PENDING',
            completed_at = NULL
        WHERE directory_id = ?
        """,
        (directory_id,),
    )

    conn.commit()

def get_in_progress_directories(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[sqlite3.Row]:

    return conn.execute(
        """
        SELECT *
        FROM directories
        WHERE run_id = ?
          AND status = 'IN_PROGRESS'
        ORDER BY directory_id
        """,
        (run_id,),
    ).fetchall()

def get_directory_file_counts(
    conn: sqlite3.Connection,
    directory_id: int,
) -> dict[str, int]:

    rows = conn.execute(
        """
        SELECT
            status,
            COUNT(*) AS count
        FROM files
        WHERE directory_id = ?
        GROUP BY status
        """,
        (directory_id,),
    ).fetchall()

    result = {
        "PENDING": 0,
        "IN_PROGRESS": 0,
        "COMPLETED": 0,
        "FAILED": 0,
    }

    for row in rows:

        status = str(row["status"])

        if status in result:
            result[status] = int(row["count"])

    return result

def get_run_file_counts(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, int]:

    rows = conn.execute(
        """
        SELECT
            status,
            COUNT(*) AS count
        FROM files
        WHERE run_id = ?
        GROUP BY status
        """,
        (run_id,),
    ).fetchall()

    result = {
        "PENDING": 0,
        "IN_PROGRESS": 0,
        "COMPLETED": 0,
        "FAILED": 0,
    }

    for row in rows:

        status = str(row["status"])

        if status in result:
            result[status] = int(row["count"])

    return result

def get_run_directory_counts(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, int]:

    rows = conn.execute(
        """
        SELECT
            status,
            COUNT(*) AS count
        FROM directories
        WHERE run_id = ?
        GROUP BY status
        """,
        (run_id,),
    ).fetchall()

    result = {
        "PENDING": 0,
        "IN_PROGRESS": 0,
        "COMPLETED": 0,
        "FAILED": 0,
    }

    for row in rows:

        status = str(row["status"])

        if status in result:
            result[status] = int(row["count"])

    return result



def checkpoint_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    reason: str,
    created_at: float | None = None,
) -> dict[str, int | float | str]:
    """Persist a durable application checkpoint in one SQLite transaction.

    File/job state is already made durable by the existing state-transition
    functions. This transaction records one consistent snapshot of those
    states together with the run's checkpoint metadata.
    """
    if conn.in_transaction:
        conn.commit()

    now = time.time() if created_at is None else created_at
    file_summary = get_run_summary(conn, run_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE refresh_runs
            SET
                last_checkpoint_at = ?,
                checkpoint_count = checkpoint_count + 1,
                checkpoint_files = ?,
                checkpoint_bytes = ?
            WHERE run_id = ?
            """,
            (
                now,
                file_summary["completed_files"],
                file_summary["completed_bytes"],
                run_id,
            ),
        )

        conn.execute(
            """
            INSERT INTO run_checkpoints(
                run_id, created_at, reason,
                total_files, pending_files, in_progress_files,
                completed_files, failed_files,
                total_bytes, completed_bytes, failed_bytes,
                pending_directories, in_progress_directories,
                completed_directories, failed_directories
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                now,
                reason,
                file_summary["total_files"],
                file_summary["pending_files"],
                file_summary["in_progress_files"],
                file_summary["completed_files"],
                file_summary["failed_files"],
                file_summary["total_bytes"],
                file_summary["completed_bytes"],
                file_summary["failed_bytes"],
                file_summary["pending_directories"],
                file_summary["in_progress_directories"],
                file_summary["completed_directories"],
                file_summary["failed_directories"],
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "run_id": run_id,
        "created_at": now,
        "reason": reason,
        **file_summary,
    }


def get_latest_checkpoint(
    conn: sqlite3.Connection,
    run_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
        FROM run_checkpoints
        WHERE run_id = ?
        ORDER BY checkpoint_id DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()


def commit(
    conn: sqlite3.Connection,
) -> None:

    conn.commit()
