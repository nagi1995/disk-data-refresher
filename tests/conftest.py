from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import refresh_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "refresh.db"


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    connection = refresh_db.connect(db_path)
    refresh_db.initialize_database(connection)

    yield connection

    connection.close()


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    root = tmp_path / "Archive"
    root.mkdir()

    return root


def write_file(
    root: Path,
    relative_path: str,
    data: bytes,
) -> Path:

    path = root / relative_path
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    path.write_bytes(data)

    return path
