from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import refresh_app
import refresh_db
from refresh_controller import CheckpointManager
from refresh_logging import configure_logging


def test_checkpoint_snapshot_is_transactionally_persisted(tmp_path: Path) -> None:
    db = tmp_path / "refresh.db"
    root = tmp_path / "archive"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")

    conn = refresh_db.connect(db)
    try:
        refresh_db.initialize_database(conn)
        run_id = refresh_db.create_run(
            conn, root, 1.0, status="READY"
        )
        refresh_db.insert_directories(
            conn,
            [(run_id, ".", 0, "PENDING", 1.0)],
        )
        directory_id = conn.execute(
            "SELECT directory_id FROM directories WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        refresh_db.insert_files(
            conn,
            [(run_id, directory_id, "a.txt", 5, 1, "PENDING", 1.0)],
        )

        snapshot = refresh_db.checkpoint_run(
            conn, run_id, reason="test"
        )
        assert snapshot["total_files"] == 1
        assert snapshot["pending_files"] == 1

        row = refresh_db.get_latest_checkpoint(conn, run_id)
        assert row is not None
        assert row["reason"] == "test"
        assert row["pending_files"] == 1

        run = refresh_db.get_run(conn, run_id)
        assert run["checkpoint_count"] == 1
        assert run["last_checkpoint_at"] is not None
    finally:
        conn.close()


def test_checkpoint_manager_triggers_on_file_count(tmp_path: Path) -> None:
    db = tmp_path / "refresh.db"
    root = tmp_path / "archive"
    root.mkdir()

    conn = refresh_db.connect(db)
    try:
        refresh_db.initialize_database(conn)
        run_id = refresh_db.create_run(conn, root, 1.0, status="RUNNING")
        refresh_db.insert_directories(
            conn,
            [(run_id, ".", 0, "PENDING", 1.0)],
        )
        manager = CheckpointManager(
            conn,
            run_id,
            checkpoint_files=2,
            checkpoint_seconds=3600,
        )
        manager.note_file(success=True, size_bytes=10)
        assert refresh_db.get_latest_checkpoint(conn, run_id) is None
        manager.note_file(success=True, size_bytes=20)
        row = refresh_db.get_latest_checkpoint(conn, run_id)
        assert row is not None
        assert row["reason"] == "file-count"
    finally:
        conn.close()


def test_event_logging_is_human_readable(tmp_path: Path) -> None:
    log_path = tmp_path / "refresh.log"
    logger = configure_logging(log_path)
    logger.info("TEST EVENT | run=17 | file=3")
    for handler in logger.handlers:
        handler.flush()

    text = log_path.read_text(encoding="utf-8")
    assert "| INFO | TEST EVENT | run=17 | file=3" in text


def test_end_to_end_checkpoint_and_logging(tmp_path: Path) -> None:
    db = tmp_path / "refresh.db"
    root = tmp_path / "archive"
    root.mkdir()
    (root / "one.txt").write_text("hello world", encoding="utf-8")
    log_path = tmp_path / "run.log"
    configure_logging(log_path)

    run_id = refresh_app.create_new_run(
        db_path=db,
        root=root,
        depth=0,
    )

    result = refresh_app.resume_run(
        db_path=db,
        run_id=run_id,
        checkpoint_files=1,
        checkpoint_seconds=3600,
    )
    assert result.files_completed == 1

    conn = refresh_db.connect(db)
    try:
        run = refresh_db.get_run(conn, run_id)
        checkpoint = refresh_db.get_latest_checkpoint(conn, run_id)
        assert run["status"] == "COMPLETED"
        assert run["checkpoint_count"] >= 1
        assert checkpoint is not None
        assert checkpoint["completed_files"] == 1
    finally:
        conn.close()

    text = log_path.read_text(encoding="utf-8")
    assert "RUN INVENTORIED" in text
    assert "RUN RESUMED" in text
    assert "FILE STARTED" in text
    assert "FILE COMPLETED" in text
    assert "CHECKPOINT" in text
    assert "RUN COMPLETED" in text

def test_logging_can_be_reconfigured(
    tmp_path: Path,
) -> None:

    first = tmp_path / "first.log"
    second = tmp_path / "second.log"

    logger = configure_logging(first)
    logger.info("FIRST")

    for handler in logger.handlers:
        handler.flush()

    logger = configure_logging(second)
    logger.info("SECOND")

    for handler in logger.handlers:
        handler.flush()

    first_text = first.read_text(
        encoding="utf-8"
    )

    second_text = second.read_text(
        encoding="utf-8"
    )

    assert "FIRST" in first_text
    assert "SECOND" not in first_text

    assert "SECOND" in second_text
    assert "FIRST" not in second_text
