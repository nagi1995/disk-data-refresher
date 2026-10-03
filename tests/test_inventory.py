from __future__ import annotations

from pathlib import Path

import pytest

import inventory
import refresh_db
from inventory import (
    InventoryError,
    ROOT_DIRECTORY,
    build_inventory,
)


def test_empty_directory(
    conn,
    archive: Path,
):

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    result = build_inventory(
        conn=conn,
        run_id=run_id,
        root=archive,
        depth=2,
    )

    assert result["files"] == 0
    assert result["total_bytes"] == 0

    directories = conn.execute(
        """
        SELECT relative_path
        FROM directories
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchall()

    assert len(directories) == 1
    assert directories[0]["relative_path"] == ROOT_DIRECTORY


def test_root_level_files_are_in_root_job(
    conn,
    archive: Path,
):

    (archive / "A.txt").write_bytes(b"A")
    (archive / "B.txt").write_bytes(b"BBBB")

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    result = build_inventory(
        conn=conn,
        run_id=run_id,
        root=archive,
        depth=2,
    )

    assert result["files"] == 2
    assert result["total_bytes"] == 5

    rows = conn.execute(
        """
        SELECT
            files.relative_path,
            directories.relative_path AS job
        FROM files
        JOIN directories
          ON directories.directory_id = files.directory_id
        WHERE files.run_id = ?
        ORDER BY files.relative_path
        """,
        (run_id,),
    ).fetchall()

    assert [row["relative_path"] for row in rows] == [
        "A.txt",
        "B.txt",
    ]

    assert all(
        row["job"] == ROOT_DIRECTORY
        for row in rows
    )


def test_depth_two_creates_expected_jobs(
    conn,
    archive: Path,
):

    paths = [
        "Photos/2024/a.jpg",
        "Photos/2025/b.jpg",
        "Videos/Movies/movie.mkv",
        "Documents/readme.txt",
    ]

    for relative in paths:
        path = archive / relative
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        path.write_bytes(b"x")

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    build_inventory(
        conn=conn,
        run_id=run_id,
        root=archive,
        depth=2,
    )

    rows = conn.execute(
        """
        SELECT relative_path
        FROM directories
        WHERE run_id = ?
        ORDER BY relative_path
        """,
        (run_id,),
    ).fetchall()

    assert [
        row["relative_path"]
        for row in rows
    ] == [
        "<ROOT>",
        "Documents",
        "Photos/2024",
        "Photos/2025",
        "Videos/Movies",
    ]

def test_depth_two_job_assignment(
    conn,
    archive: Path,
):

    files = {
        "Photos/2024/a.jpg": b"a",
        "Photos/2025/b.jpg": b"b",
        "Videos/Movies/movie.mkv": b"movie",
        "Documents/readme.txt": b"readme",
    }

    for relative, data in files.items():
        path = archive / relative
        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )
        path.write_bytes(data)

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    build_inventory(
        conn=conn,
        run_id=run_id,
        root=archive,
        depth=2,
    )

    rows = conn.execute(
        """
        SELECT
            files.relative_path,
            directories.relative_path AS job
        FROM files
        JOIN directories
          ON directories.directory_id = files.directory_id
        WHERE files.run_id = ?
        ORDER BY files.relative_path
        """,
        (run_id,),
    ).fetchall()

    mapping = {
        row["relative_path"]: row["job"]
        for row in rows
    }

    assert mapping == {
        "Documents/readme.txt": "Documents/readme.txt"
        if False else "Documents",
        "Photos/2024/a.jpg": "Photos/2024",
        "Photos/2025/b.jpg": "Photos/2025",
        "Videos/Movies/movie.mkv": "Videos/Movies",
    }


def test_inventory_inserts_file_rows_in_batches(
    conn,
    archive: Path,
    monkeypatch,
):

    for index in range(5):
        (archive / f"file-{index}.txt").write_text(
            str(index),
            encoding="utf-8",
        )

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    batch_sizes: list[int] = []
    original_insert_files = inventory.insert_files

    def record_batches(conn, rows, *, commit=True):
        batch = list(rows)
        batch_sizes.append(len(batch))
        original_insert_files(
            conn,
            batch,
            commit=commit,
        )

    monkeypatch.setattr(
        inventory,
        "insert_files",
        record_batches,
    )

    result = build_inventory(
        conn=conn,
        run_id=run_id,
        root=archive,
        depth=1,
        file_batch_size=2,
    )

    assert result["files"] == 5
    assert batch_sizes == [2, 2, 1]

    count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]

    assert count == 5


def test_batched_inventory_preserves_job_assignment(
    conn,
    archive: Path,
):

    files = {
        "root.txt": b"root",
        "Photos/2024/a.jpg": b"a",
        "Photos/2025/b.jpg": b"b",
        "Videos/Movies/movie.mkv": b"movie",
    }

    for relative_path, contents in files.items():
        path = archive / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    build_inventory(
        conn=conn,
        run_id=run_id,
        root=archive,
        depth=2,
        file_batch_size=1,
    )

    rows = conn.execute(
        """
        SELECT
            files.relative_path,
            directories.relative_path AS job
        FROM files
        JOIN directories
          ON directories.directory_id = files.directory_id
        WHERE files.run_id = ?
        ORDER BY files.relative_path
        """,
        (run_id,),
    ).fetchall()

    assert {
        row["relative_path"]: row["job"]
        for row in rows
    } == {
        "root.txt": ROOT_DIRECTORY,
        "Photos/2024/a.jpg": "Photos/2024",
        "Photos/2025/b.jpg": "Photos/2025",
        "Videos/Movies/movie.mkv": "Videos/Movies",
    }


@pytest.mark.parametrize("file_batch_size", [0, -1])
def test_inventory_rejects_non_positive_file_batch_size(
    conn,
    archive: Path,
    file_batch_size: int,
):

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    with pytest.raises(
        InventoryError,
        match="file_batch_size must be greater than zero",
    ):
        build_inventory(
            conn=conn,
            run_id=run_id,
            root=archive,
            depth=1,
            file_batch_size=file_batch_size,
        )


def test_inventory_rolls_back_when_a_batch_insert_fails(
    conn,
    archive: Path,
    monkeypatch,
):

    for index in range(3):
        (archive / f"file-{index}.txt").write_text(
            str(index),
            encoding="utf-8",
        )

    run_id = refresh_db.create_run(
        conn=conn,
        root_path=archive,
        started_at=0,
        status="CREATING",
    )

    original_insert_files = inventory.insert_files
    calls = 0

    def fail_after_second_batch(conn, rows, *, commit=True):
        nonlocal calls
        calls += 1
        original_insert_files(
            conn,
            rows,
            commit=commit,
        )

        if calls == 2:
            raise RuntimeError("simulated batch write failure")

    monkeypatch.setattr(
        inventory,
        "insert_files",
        fail_after_second_batch,
    )

    with pytest.raises(
        RuntimeError,
        match="simulated batch write failure",
    ):
        build_inventory(
            conn=conn,
            run_id=run_id,
            root=archive,
            depth=1,
            file_batch_size=1,
        )

    file_count = conn.execute(
        "SELECT COUNT(*) FROM files WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]

    directory_count = conn.execute(
        "SELECT COUNT(*) FROM directories WHERE run_id = ?",
        (run_id,),
    ).fetchone()[0]

    assert file_count == 0
    assert directory_count == 0


