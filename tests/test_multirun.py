from __future__ import annotations

import refresh_app
import refresh_db


def test_second_run_is_independent(
    tmp_path,
):

    root = tmp_path / "Archive"
    root.mkdir()

    (root / "A.txt").write_text("A")
    (root / "B.txt").write_text("B")
    (root / "C.txt").write_text("C")

    db = tmp_path / "refresh.db"

    # --------------------------------------------------------
    # Run #1
    # --------------------------------------------------------

    run1 = refresh_app.create_new_run(
        db_path=db,
        root=root,
        depth=2,
    )

    refresh_app.resume_run(
        db_path=db,
        run_id=run1,
    )

    # --------------------------------------------------------
    # Change filesystem after Run #1.
    # --------------------------------------------------------

    (root / "B.txt").unlink()

    new_directory = root / "NewDirectory"
    new_directory.mkdir()

    (new_directory / "A.txt").write_text("A")

    (root / "D.txt").write_text("D")

    # --------------------------------------------------------
    # Run #2
    # --------------------------------------------------------

    run2 = refresh_app.create_new_run(
        db_path=db,
        root=root,
        depth=2,
    )

    assert run2 != run1

    conn = refresh_db.connect(db)

    try:

        run1_files = conn.execute(
            """
            SELECT relative_path
            FROM files
            WHERE run_id = ?
            ORDER BY relative_path
            """,
            (run1,),
        ).fetchall()

        run2_files = conn.execute(
            """
            SELECT relative_path
            FROM files
            WHERE run_id = ?
            ORDER BY relative_path
            """,
            (run2,),
        ).fetchall()

        assert [
            row["relative_path"]
            for row in run1_files
        ] == [
            "A.txt",
            "B.txt",
            "C.txt",
        ]

        assert [
            row["relative_path"]
            for row in run2_files
        ] == [
            "A.txt",
            "C.txt",
            "D.txt",
            "NewDirectory/A.txt",
        ]

    finally:
        conn.close()

