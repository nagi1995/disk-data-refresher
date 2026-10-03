from __future__ import annotations

import hashlib
from pathlib import Path

import refresh_app
import refresh_db


def file_hash(path: Path) -> str:

    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def test_start_and_resume(
    tmp_path: Path,
):

    root = tmp_path / "Archive"
    root.mkdir()

    (root / "A.txt").write_text("AAAA")

    photos = root / "Photos"
    photos.mkdir()

    (photos / "B.txt").write_text("BBBB")

    db = tmp_path / "refresh.db"

    run_id = refresh_app.create_new_run(
        db_path=db,
        root=root,
        depth=1,
    )

    assert run_id > 0

    conn = refresh_db.connect(db)

    try:
        run = refresh_db.get_run(
            conn,
            run_id,
        )

        assert run["status"] == "READY"

        counts = refresh_db.get_run_file_counts(
            conn,
            run_id,
        )

        assert counts["PENDING"] == 2
        assert counts["COMPLETED"] == 0

    finally:
        conn.close()

    result = refresh_app.resume_run(
        db_path=db,
        run_id=run_id,
    )

    assert result.files_completed == 2
    assert result.files_failed == 0

    conn = refresh_db.connect(db)

    try:
        run = refresh_db.get_run(
            conn,
            run_id,
        )

        assert run["status"] == "COMPLETED"

        counts = refresh_db.get_run_file_counts(
            conn,
            run_id,
        )

        assert counts["COMPLETED"] == 2
        assert counts["FAILED"] == 0
        assert counts["PENDING"] == 0

    finally:
        conn.close()

    assert file_hash(root / "A.txt") == hashlib.sha256(
        b"AAAA"
    ).hexdigest()

    assert file_hash(
        root / "Photos" / "B.txt"
    ) == hashlib.sha256(
        b"BBBB"
    ).hexdigest()


def test_max_files_pauses_run(
    tmp_path: Path,
):

    root = tmp_path / "Archive"
    root.mkdir()

    for name in ["A.txt", "B.txt", "C.txt"]:
        (root / name).write_text(name)

    db = tmp_path / "refresh.db"

    run_id = refresh_app.create_new_run(
        db_path=db,
        root=root,
        depth=1,
    )

    result = refresh_app.resume_run(
        db_path=db,
        run_id=run_id,
        max_files=1,
    )

    assert result.files_processed == 1

    conn = refresh_db.connect(db)

    try:
        run = refresh_db.get_run(
            conn,
            run_id,
        )

        assert run["status"] == "PAUSED"

        counts = refresh_db.get_run_file_counts(
            conn,
            run_id,
        )

        assert counts["COMPLETED"] == 1
        assert counts["PENDING"] == 2

    finally:
        conn.close()

    # Continue the same run.
    result = refresh_app.resume_run(
        db_path=db,
        run_id=run_id,
    )

    assert result.files_completed == 2

    conn = refresh_db.connect(db)

    try:
        run = refresh_db.get_run(
            conn,
            run_id,
        )

        assert run["status"] == "COMPLETED"

    finally:
        conn.close()

def test_in_progress_file_is_recovered(
    tmp_path,
):

    root = tmp_path / "Archive"
    root.mkdir()

    source = root / "A.txt"
    source.write_text("original")

    db = tmp_path / "refresh.db"

    run_id = refresh_app.create_new_run(
        db_path=db,
        root=root,
        depth=1,
    )

    conn = refresh_db.connect(db)

    try:

        row = conn.execute(
            """
            SELECT file_id
            FROM files
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        refresh_db.mark_file_in_progress(
            conn=conn,
            file_id=row["file_id"],
            started_at=1.0,
            attempts=1,
        )

    finally:
        conn.close()

    # Simulate a process crash here.

    result = refresh_app.resume_run(
        db_path=db,
        run_id=run_id,
    )

    assert result.files_completed == 1

    conn = refresh_db.connect(db)

    try:

        row = conn.execute(
            """
            SELECT
                status,
                attempts
            FROM files
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        assert row["status"] == "COMPLETED"

        # Recovery creates another attempt.
        assert row["attempts"] >= 2

    finally:
        conn.close()


