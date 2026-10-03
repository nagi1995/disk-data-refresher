from __future__ import annotations

import time

import refresh_db


def test_file_failure_creates_history(
    conn,
    archive,
):

    file_path = archive / "test.txt"
    file_path.write_text("hello")

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=time.time(),
        status="READY",
    )

    refresh_db.insert_directories(
        conn,
        [
            (
                run_id,
                "<ROOT>",
                0,
                "PENDING",
                time.time(),
            )
        ],
    )

    directory = conn.execute(
        """
        SELECT directory_id
        FROM directories
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    refresh_db.insert_files(
        conn,
        [
            (
                run_id,
                directory["directory_id"],
                "test.txt",
                file_path.stat().st_size,
                file_path.stat().st_mtime_ns,
                "PENDING",
                time.time(),
            )
        ],
    )

    row = conn.execute(
        """
        SELECT file_id
        FROM files
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    refresh_db.mark_file_in_progress(
        conn,
        row["file_id"],
        time.time(),
        1,
    )

    refresh_db.mark_file_failed(
        conn,
        row["file_id"],
        "TEST_FAILURE",
        "intentional test failure",
    )

    file_row = refresh_db.get_file(
        conn,
        row["file_id"],
    )

    assert file_row["status"] == "FAILED"
    assert file_row["attempts"] == 1
    assert file_row["last_error_code"] == "TEST_FAILURE"

    failures = refresh_db.get_failure_history(
        conn,
        run_id,
    )

    assert len(failures) == 1
    assert failures[0]["error_code"] == "TEST_FAILURE"


def test_retry_preserves_attempt_count(
    conn,
    archive,
):

    file_path = archive / "test.txt"
    file_path.write_text("hello")

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=time.time(),
        status="READY",
    )

    refresh_db.insert_directories(
        conn,
        [
            (
                run_id,
                "<ROOT>",
                0,
                "FAILED",
                time.time(),
            )
        ],
    )

    directory = conn.execute(
        """
        SELECT directory_id
        FROM directories
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    refresh_db.insert_files(
        conn,
        [
            (
                run_id,
                directory["directory_id"],
                "test.txt",
                file_path.stat().st_size,
                file_path.stat().st_mtime_ns,
                "FAILED",
                time.time(),
            )
        ],
    )

    file_row = conn.execute(
        """
        SELECT file_id
        FROM files
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    refresh_db.mark_file_in_progress(
        conn,
        file_row["file_id"],
        time.time(),
        3,
    )

    refresh_db.mark_file_failed(
        conn,
        file_row["file_id"],
        "TEST_FAILURE",
        "failure",
    )

    count = refresh_db.reset_failed_files_to_pending(
        conn,
        run_id,
    )

    assert count == 1

    row = refresh_db.get_file(
        conn,
        file_row["file_id"],
    )

    assert row["status"] == "PENDING"
    assert row["attempts"] == 3

    failures = refresh_db.get_failure_history(
        conn,
        run_id,
    )

    assert len(failures) == 1

