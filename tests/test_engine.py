from __future__ import annotations

import hashlib
from pathlib import Path

import refresh_engine


def sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def test_successful_refresh(
    archive: Path,
):

    source = archive / "test.bin"
    source.write_bytes(
        b"hello world" * 1000
    )

    expected_size = source.stat().st_size
    expected_mtime = source.stat().st_mtime_ns
    before_hash = sha256(source)

    result = refresh_engine.refresh_one_file(
        root=archive,
        relative_path="test.bin",
        expected_size=expected_size,
        expected_mtime_ns=expected_mtime,
    )

    assert result["status"] == refresh_engine.COMPLETED
    assert result["sha256"] == before_hash

    after_hash = sha256(source)

    assert after_hash == before_hash
    assert source.stat().st_size == expected_size


def test_missing_source_fails(
    archive: Path,
):

    result = refresh_engine.refresh_one_file(
        root=archive,
        relative_path="missing.bin",
        expected_size=123,
        expected_mtime_ns=123,
    )

    assert result["status"] == refresh_engine.FAILED
    assert result["error_code"] == refresh_engine.MISSING_SOURCE


def test_size_change_fails(
    archive: Path,
):

    source = archive / "test.bin"
    source.write_bytes(b"original")

    expected_mtime = source.stat().st_mtime_ns

    result = refresh_engine.refresh_one_file(
        root=archive,
        relative_path="test.bin",
        expected_size=999999,
        expected_mtime_ns=expected_mtime,
    )

    assert result["status"] == refresh_engine.FAILED
    assert result["error_code"] == refresh_engine.SOURCE_CHANGED

    assert source.read_bytes() == b"original"


def test_mtime_change_fails(
    archive: Path,
):

    source = archive / "test.bin"
    source.write_bytes(b"original")

    original_size = source.stat().st_size

    original_mtime = source.stat().st_mtime_ns

    source.touch()

    # It is possible for some filesystems to preserve the
    # timestamp resolution. Force a clearly different mtime.
    import os

    os.utime(
        source,
        ns=(
            source.stat().st_atime_ns,
            original_mtime + 10_000_000_000,
        ),
    )

    result = refresh_engine.refresh_one_file(
        root=archive,
        relative_path="test.bin",
        expected_size=original_size,
        expected_mtime_ns=original_mtime,
    )

    assert result["status"] == refresh_engine.FAILED
    assert result["error_code"] == refresh_engine.SOURCE_CHANGED

    assert source.read_bytes() == b"original"


def test_temp_file_is_removed_after_success(
    archive: Path,
):

    source = archive / "test.bin"
    source.write_bytes(b"test data")

    result = refresh_engine.refresh_one_file(
        root=archive,
        relative_path="test.bin",
        expected_size=source.stat().st_size,
        expected_mtime_ns=source.stat().st_mtime_ns,
    )

    assert result["status"] == refresh_engine.COMPLETED

    temp = source.with_name(
        source.name + refresh_engine.TEMP_SUFFIX
    )

    assert not temp.exists()


def test_symlink_is_rejected(
    archive: Path,
):

    target = archive / "target.txt"
    target.write_text("target")

    link = archive / "link.txt"

    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        # Windows may require developer mode / privileges.
        return

    result = refresh_engine.refresh_one_file(
        root=archive,
        relative_path="link.txt",
        expected_size=target.stat().st_size,
        expected_mtime_ns=target.stat().st_mtime_ns,
    )

    assert result["status"] == refresh_engine.FAILED
    assert result["error_code"] == refresh_engine.MISSING_SOURCE

