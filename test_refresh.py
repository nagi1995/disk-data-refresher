
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

import pytest

import refresh



def write_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """
    IMPORTANT:
    Every test uses pytest's temporary directory.

    Nothing here points at a real HDD/archive.
    """
    root = tmp_path / "archive"
    root.mkdir()

    write_file(root / "a.bin", b"A" * 100)
    write_file(root / "b.txt", b"hello archive")
    write_file(root / "sub" / "c.dat", b"subdirectory data")

    (root / "empty").mkdir()

    return root


def make_manifest(root: Path, tmp_path: Path):
    manifest = refresh.create_manifest(root)
    manifest_path = tmp_path / "manifest.json"
    refresh.save_manifest(manifest_path, manifest)
    return manifest, manifest_path


# ---------------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------------

def test_hash_and_signature(archive):
    path = archive / "a.bin"

    digest = refresh.sha256_of(path)
    size, mtime = refresh.get_file_signature(path)

    assert len(digest) == 64
    assert size == 100
    assert isinstance(mtime, int)


def test_readonly_helpers(archive):
    path = archive / "a.bin"

    original = refresh.is_readonly(path)

    refresh.set_readonly(path, True)
    assert refresh.is_readonly(path)

    refresh.set_readonly(path, False)
    assert not refresh.is_readonly(path)

    refresh.set_readonly(path, original)


def test_copy_and_metadata(archive, tmp_path):
    source = archive / "a.bin"
    target = tmp_path / "copy.bin"

    refresh.copy_writethrough(source, target)
    refresh.preserve_metadata(source, target)

    assert target.read_bytes() == source.read_bytes()


def test_temp_path(archive):
    temp = refresh.temp_path_for(archive / "a.bin")

    assert temp.name == "a.bin.refreshtmp"


def test_manifest_path(archive):
    path = refresh.manifest_path_for(archive)

    assert path.name == "refresh-manifest.json"


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def test_create_manifest(archive):
    manifest = refresh.create_manifest(archive)

    assert manifest["format_version"] == refresh.MANIFEST_VERSION
    assert "a.bin" in manifest["files"]
    assert "b.txt" in manifest["files"]
    assert "sub/c.dat" in manifest["files"]
    assert "sub" in manifest["directories"]

    assert manifest["files"]["a.bin"]["status"] == refresh.STATUS_PENDING


def test_save_and_load_manifest(archive, tmp_path):
    manifest = refresh.create_manifest(archive)
    path = tmp_path / "manifest.json"

    refresh.save_manifest(path, manifest)

    loaded = refresh.load_manifest(path)

    assert loaded["root"] == manifest["root"]
    assert loaded["format_version"] == refresh.MANIFEST_VERSION


def test_bad_manifest_version(tmp_path):
    path = tmp_path / "bad.json"

    path.write_text(
        json.dumps({"format_version": 999}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        refresh.load_manifest(path)


def test_manifest_wrong_root(archive, tmp_path):
    manifest = refresh.create_manifest(archive)

    other = tmp_path / "other"
    other.mkdir()

    with pytest.raises(ValueError):
        refresh.validate_manifest_root(other, manifest)


def test_skipped_directories(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()

    for directory in refresh.SKIP_DIRS:
        d = root / directory
        d.mkdir()
        write_file(d / "hidden.dat", b"hidden")

    manifest = refresh.create_manifest(root)

    for relative in manifest["files"]:
        assert not any(
            relative.startswith(directory + "/")
            for directory in refresh.SKIP_DIRS
        )


def test_symlink_not_manifested(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()

    real = root / "real.txt"
    write_file(real, b"real")

    link = root / "link.txt"

    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")

    manifest = refresh.create_manifest(root)

    assert "real.txt" in manifest["files"]
    assert "link.txt" not in manifest["files"]


# ---------------------------------------------------------------------------
# Source validation
# ---------------------------------------------------------------------------

def test_source_validation_passes(archive):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    ok, reason = refresh.validate_source_against_manifest(
        archive / "a.bin",
        expected,
    )

    assert ok
    assert reason == ""


def test_source_validation_detects_size_change(archive):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    (archive / "a.bin").write_bytes(b"short")

    ok, reason = refresh.validate_source_against_manifest(
        archive / "a.bin",
        expected,
    )

    assert not ok
    assert reason == refresh.FAIL_MANIFEST_MISMATCH


def test_source_validation_detects_hash_change(archive):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    # Same size, different bytes.
    (archive / "a.bin").write_bytes(b"B" * 100)

    ok, reason = refresh.validate_source_against_manifest(
        archive / "a.bin",
        expected,
    )

    assert not ok
    assert reason == refresh.FAIL_MANIFEST_MISMATCH


def test_source_validation_detects_missing_file(archive):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    (archive / "a.bin").unlink()

    ok, reason = refresh.validate_source_against_manifest(
        archive / "a.bin",
        expected,
    )

    assert not ok
    assert reason == refresh.FAIL_MISSING_SOURCE


# ---------------------------------------------------------------------------
# Temporary validation
# ---------------------------------------------------------------------------

def test_temp_validation_passes(archive, tmp_path):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    source = archive / "a.bin"
    temp = refresh.temp_path_for(source)

    refresh.copy_writethrough(source, temp)
    refresh.preserve_metadata(source, temp)

    ok, reason = refresh.validate_temp(
        source,
        temp,
        expected,
    )

    assert ok
    assert reason == ""

    temp.unlink()


def test_temp_validation_detects_corruption(archive):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    source = archive / "a.bin"
    temp = refresh.temp_path_for(source)

    temp.write_bytes(b"CORRUPT")

    ok, reason = refresh.validate_temp(
        source,
        temp,
        expected,
    )

    assert not ok
    assert reason == refresh.FAIL_TEMP_VERIFY


def test_temp_validation_missing(archive):
    manifest = refresh.create_manifest(archive)
    expected = manifest["files"]["a.bin"]

    source = archive / "a.bin"
    temp = refresh.temp_path_for(source)

    ok, reason = refresh.validate_temp(
        source,
        temp,
        expected,
    )

    assert not ok
    assert reason == refresh.FAIL_TEMP_VERIFY


# ---------------------------------------------------------------------------
# CRITICAL SAFETY TESTS
# ---------------------------------------------------------------------------

def test_success_replaces_source_only_after_validation(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    replace_calls = []

    real_replace = refresh.os.replace

    def tracking_replace(src, dst):
        replace_calls.append((src, dst))
        return real_replace(src, dst)

    monkeypatch.setattr(
        refresh.os,
        "replace",
        tracking_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is True
    assert len(replace_calls) == 1
    assert source.exists()
    assert source.read_bytes() == original
    assert not refresh.temp_path_for(source).exists()
    assert (
        manifest["files"]["a.bin"]["status"]
        == refresh.STATUS_COMPLETED
    )


def test_no_space_does_not_call_replace(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 0,
    )

    replace_called = False

    def forbidden_replace(*args):
        nonlocal replace_called
        replace_called = True
        raise AssertionError("os.replace MUST NOT be called")

    monkeypatch.setattr(
        refresh.os,
        "replace",
        forbidden_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False
    assert replace_called is False
    assert source.read_bytes() == original
    assert manifest["files"]["a.bin"]["last_error"] == refresh.FAIL_NO_SPACE


def test_source_changed_before_refresh_never_replaces(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"

    # Simulate user modification.
    changed = b"USER DATA - DO NOT OVERWRITE"
    source.write_bytes(changed)

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    def forbidden_replace(*args):
        raise AssertionError("os.replace MUST NOT be called")

    monkeypatch.setattr(
        refresh.os,
        "replace",
        forbidden_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False
    assert source.read_bytes() == changed
    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_MANIFEST_MISMATCH
    )


def test_source_changes_after_copy_never_replaces(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    real_copy = refresh.copy_writethrough

    def copy_then_modify(src, dst):
        real_copy(src, dst)
        src.write_bytes(b"USER CHANGED SOURCE")

    monkeypatch.setattr(
        refresh,
        "copy_writethrough",
        copy_then_modify,
    )

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    def forbidden_replace(*args):
        raise AssertionError("os.replace MUST NOT be called")

    monkeypatch.setattr(
        refresh.os,
        "replace",
        forbidden_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False
    assert source.read_bytes() == b"USER CHANGED SOURCE"
    assert source.read_bytes() != original
    assert not refresh.temp_path_for(source).exists()

    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_SOURCE_CHANGED
    )


def test_temp_hash_mismatch_never_replaces(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    def corrupt_copy(_src, dst):
        dst.write_bytes(b"CORRUPTED TARGET")
        refresh.fsync_file(dst)

    monkeypatch.setattr(
        refresh,
        "copy_writethrough",
        corrupt_copy,
    )

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    def forbidden_replace(*args):
        raise AssertionError("os.replace MUST NOT be called")

    monkeypatch.setattr(
        refresh.os,
        "replace",
        forbidden_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False

    # Most important assertion:
    # ORIGINAL IS STILL THERE AND UNCHANGED.
    assert source.exists()
    assert source.read_bytes() == original

    # Temp is our disposable artifact.
    assert not refresh.temp_path_for(source).exists()

    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_TEMP_VERIFY
    )


def test_metadata_mismatch_never_replaces(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    real_copy = refresh.copy_writethrough

    def copy_without_metadata(src, dst):
        real_copy(src, dst)
        # Deliberately change mtime.
        os.utime(
            dst,
            ns=(
                src.stat().st_atime_ns,
                src.stat().st_mtime_ns + 1000000,
            ),
        )

    monkeypatch.setattr(
        refresh,
        "copy_writethrough",
        copy_without_metadata,
    )

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    def forbidden_replace(*args):
        raise AssertionError("os.replace MUST NOT be called")

    monkeypatch.setattr(
        refresh.os,
        "replace",
        forbidden_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False
    assert source.read_bytes() == original
    assert not refresh.temp_path_for(source).exists()
    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_METADATA_VERIFY
    )


def test_preexisting_temp_is_never_deleted_or_replaced(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    temp = refresh.temp_path_for(source)

    temp_data = b"OLD TEMP THAT MUST NOT BE TOUCHED"
    temp.write_bytes(temp_data)

    original = source.read_bytes()

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False
    assert source.read_bytes() == original

    # We deliberately don't delete a pre-existing temp because we don't know
    # whether it belongs to this run.
    assert temp.exists()
    assert temp.read_bytes() == temp_data


def test_replace_failure_keeps_original_and_cleans_temp(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    monkeypatch.setattr(
        refresh,
        "RETRY_DELAY",
        0,
    )

    def failing_replace(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(
        refresh.os,
        "replace",
        failing_replace,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False

    # Replace never succeeded.
    assert source.exists()
    assert source.read_bytes() == original

    # Temp is disposable.
    assert not refresh.temp_path_for(source).exists()

    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_REPLACE
    )


def test_permission_retry_then_success(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    monkeypatch.setattr(
        refresh,
        "RETRY_DELAY",
        0,
    )

    real_copy = refresh.copy_writethrough
    calls = {"count": 0}

    def flaky_copy(src, dst):
        calls["count"] += 1

        if calls["count"] == 1:
            raise PermissionError("temporary permission failure")

        return real_copy(src, dst)

    monkeypatch.setattr(
        refresh,
        "copy_writethrough",
        flaky_copy,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is True
    assert calls["count"] == 2
    assert (
        manifest["files"]["a.bin"]["status"]
        == refresh.STATUS_COMPLETED
    )


def test_permission_exhausted_keeps_original(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    source = archive / "a.bin"
    original = source.read_bytes()

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    monkeypatch.setattr(
        refresh,
        "RETRY_DELAY",
        0,
    )

    def always_permission_error(*args):
        raise PermissionError("permission denied")

    monkeypatch.setattr(
        refresh,
        "copy_writethrough",
        always_permission_error,
    )

    result = refresh.refresh_file(
        archive,
        "a.bin",
        manifest["files"]["a.bin"],
        manifest,
        manifest_path,
    )

    assert result is False
    assert source.read_bytes() == original
    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_PERMISSION
    )


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def test_recover_in_progress_matching_source(
    archive,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    manifest["files"]["a.bin"]["status"] = (
        refresh.STATUS_IN_PROGRESS
    )

    # Simulate stale temp.
    temp = refresh.temp_path_for(archive / "a.bin")
    temp.write_bytes(b"stale temp")

    refresh.save_manifest(
        manifest_path,
        manifest,
    )

    result = refresh.recover_in_progress(
        archive,
        manifest,
        manifest_path,
    )

    assert result is True
    assert (
        manifest["files"]["a.bin"]["status"]
        == refresh.STATUS_PENDING
    )
    assert not temp.exists()


def test_recover_missing_source_is_unsafe(
    archive,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    manifest["files"]["a.bin"]["status"] = (
        refresh.STATUS_IN_PROGRESS
    )

    (archive / "a.bin").unlink()

    refresh.save_manifest(
        manifest_path,
        manifest,
    )

    result = refresh.recover_in_progress(
        archive,
        manifest,
        manifest_path,
    )

    assert result is False
    assert (
        manifest["files"]["a.bin"]["last_error"]
        == refresh.FAIL_MISSING_SOURCE
    )


def test_recover_changed_source_is_unsafe(
    archive,
    tmp_path,
):
    manifest, manifest_path = make_manifest(
        archive,
        tmp_path,
    )

    manifest["files"]["a.bin"]["status"] = (
        refresh.STATUS_IN_PROGRESS
    )

    (archive / "a.bin").write_bytes(b"CHANGED")

    refresh.save_manifest(
        manifest_path,
        manifest,
    )

    result = refresh.recover_in_progress(
        archive,
        manifest,
        manifest_path,
    )

    assert result is False
    assert (
        manifest["files"]["a.bin"]["status"]
        == refresh.STATUS_FAILED
    )


# ---------------------------------------------------------------------------
# Final archive validation
# ---------------------------------------------------------------------------

def test_validate_archive_passes(archive):
    manifest = refresh.create_manifest(archive)

    ok, errors = refresh.validate_archive(
        archive,
        manifest,
    )

    assert ok
    assert errors == []


def test_validate_archive_detects_hash_mismatch(archive):
    manifest = refresh.create_manifest(archive)

    (archive / "a.bin").write_bytes(b"BAD")

    ok, errors = refresh.validate_archive(
        archive,
        manifest,
    )

    assert not ok
    assert any(
        "Hash mismatch: a.bin" in error
        for error in errors
    )


def test_validate_archive_detects_unexpected_file(archive):
    manifest = refresh.create_manifest(archive)

    write_file(
        archive / "unexpected.txt",
        b"unexpected",
    )

    ok, errors = refresh.validate_archive(
        archive,
        manifest,
    )

    assert not ok
    assert any(
        "Unexpected file" in error
        for error in errors
    )


def test_validate_archive_detects_missing_file(archive):
    manifest = refresh.create_manifest(archive)

    (archive / "a.bin").unlink()

    ok, errors = refresh.validate_archive(
        archive,
        manifest,
    )

    assert not ok
    assert any(
        "Missing file: a.bin" in error
        for error in errors
    )


def test_validate_archive_detects_leftover_temp(archive):
    manifest = refresh.create_manifest(archive)

    temp = refresh.temp_path_for(
        archive / "a.bin"
    )

    temp.write_bytes(b"temp")

    ok, errors = refresh.validate_archive(
        archive,
        manifest,
    )

    assert not ok
    assert any(
        "Leftover temporary file" in error
        for error in errors
    )


# ---------------------------------------------------------------------------
# Whole process
# ---------------------------------------------------------------------------

def test_process_fresh_run(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest_path = tmp_path / "manifest.json"

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    result = refresh.process(
        archive,
        manifest_path,
        supplied_manifest=False,
    )

    assert result == 0

    manifest = refresh.load_manifest(
        manifest_path
    )

    assert manifest["status"] == refresh.STATUS_COMPLETED

    assert all(
        item["status"] == refresh.STATUS_COMPLETED
        for item in manifest["files"].values()
    )


def test_process_restart_revalidates_completed_files(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest_path = tmp_path / "manifest.json"

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    assert refresh.process(
        archive,
        manifest_path,
        supplied_manifest=False,
    ) == 0

    # Restart.
    assert refresh.process(
        archive,
        manifest_path,
        supplied_manifest=True,
    ) == 0


def test_process_completed_file_changed_stops(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest_path = tmp_path / "manifest.json"

    manifest = refresh.create_manifest(archive)

    manifest["files"]["a.bin"]["status"] = (
        refresh.STATUS_COMPLETED
    )

    refresh.save_manifest(
        manifest_path,
        manifest,
    )

    (archive / "a.bin").write_bytes(b"CHANGED")

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    result = refresh.process(
        archive,
        manifest_path,
        supplied_manifest=True,
    )

    assert result == 2
    saved_manifest = refresh.load_manifest(manifest_path)
    assert (
        saved_manifest["files"]["a.bin"]["status"]
        == refresh.STATUS_FAILED
    )


def test_process_failed_task_is_retried(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest_path = tmp_path / "manifest.json"

    manifest = refresh.create_manifest(archive)

    manifest["files"]["a.bin"]["status"] = (
        refresh.STATUS_FAILED
    )

    manifest["files"]["a.bin"]["last_error"] = (
        refresh.FAIL_PERMISSION
    )

    refresh.save_manifest(
        manifest_path,
        manifest,
    )

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    monkeypatch.setattr(
        refresh,
        "RETRY_DELAY",
        0,
    )

    result = refresh.process(
        archive,
        manifest_path,
        supplied_manifest=True,
    )

    assert result == 0


def test_missing_supplied_manifest_creates_new(
    archive,
    monkeypatch,
    tmp_path,
):
    manifest_path = tmp_path / "new.json"

    monkeypatch.setattr(
        refresh,
        "get_free_gb",
        lambda _path: 1000,
    )

    result = refresh.process(
        archive,
        manifest_path,
        supplied_manifest=True,
    )

    assert result == 0
    assert manifest_path.exists()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_parser_calls_argument_path():
    parser = refresh.build_parser()

    args = parser.parse_args(
        [
            "--path",
            r"E:\Archive",
            "--manifest",
            "manifest.json",
        ]
    )

    assert args.path == Path(r"E:\Archive")
    assert args.manifest == Path("manifest.json")


def test_main_fatal_error_returns_two(
    monkeypatch,
    tmp_path,
):
    def fatal(*args, **kwargs):
        raise RuntimeError("simulated fatal error")

    monkeypatch.setattr(
        refresh,
        "process",
        fatal,
    )

    result = refresh.main(
        ["--path", str(tmp_path)]
    )

    assert result == 2


# ---------------------------------------------------------------------------
# Cleanup helper
# ---------------------------------------------------------------------------

def test_remove_temp_file_is_idempotent(
    archive,
):
    temp = refresh.temp_path_for(
        archive / "a.bin"
    )

    temp.write_bytes(b"x")

    refresh.remove_temp_file(temp)
    refresh.remove_temp_file(temp)

    assert not temp.exists()

def test_partial_copy_exception_original_survives_and_temp_removed(
    tmp_path, monkeypatch
):
    root = tmp_path
    source = root / "file.bin"
    source.write_bytes(b"original")

    manifest_path = root / "manifest.json"
    manifest = refresh.create_manifest(root)
    refresh.save_manifest(manifest_path, manifest)

    original_copy = refresh.copy_writethrough
    calls = {"count": 0}

    def failing_copy(src, dst):
        calls["count"] += 1
        if calls["count"] == 1:
            dst.write_bytes(b"partial")
            raise OSError("simulated copy failure")
        return original_copy(src, dst)

    monkeypatch.setattr(refresh, "copy_writethrough", failing_copy)

    expected = manifest["files"]["file.bin"]

    result = refresh.refresh_file(
        root=root,
        relative="file.bin",
        expected=expected,
        manifest=manifest,
        manifest_path=manifest_path,
    )

    assert result is True
    assert source.read_bytes() == b"original"
    assert not list(root.glob("*.refreshtmp"))


def test_replace_fails_twice_then_succeeds_on_third_attempt(
    tmp_path, monkeypatch
):
    root = tmp_path
    source = root / "file.bin"
    source.write_bytes(b"original")

    manifest_path = root / "manifest.json"
    manifest = refresh.create_manifest(root)
    refresh.save_manifest(manifest_path, manifest)

    original_replace = refresh.os.replace
    calls = {"count": 0}

    def flaky_replace(src, dst):
        calls["count"] += 1
        if calls["count"] < 3:
            raise OSError("simulated replace failure")
        return original_replace(src, dst)

    monkeypatch.setattr(refresh.os, "replace", flaky_replace)

    expected = manifest["files"]["file.bin"]

    result = refresh.refresh_file(
        root=root,
        relative="file.bin",
        expected=expected,
        manifest=manifest,
        manifest_path=manifest_path,
    )

    assert result is True
    assert calls["count"] == 3
    assert source.exists()
    assert not list(root.glob("*.refreshtmp"))




def test_corrupt_manifest_fails_cleanly(tmp_path):
    root = tmp_path
    manifest_path = root / "manifest.json"
    manifest_path.write_text(
        "{ this is not valid json",
        encoding="utf-8",
    )

    with pytest.raises(json.JSONDecodeError):
        refresh.process(
            root,
            manifest_path,
            supplied_manifest=True,
        )

def test_read_only_source_successful_refresh_remains_read_only(
    tmp_path, monkeypatch
):
    root = tmp_path
    source = root / "file.bin"
    source.write_bytes(b"original")

    manifest_path = root / "manifest.json"
    manifest = refresh.create_manifest(root)
    refresh.save_manifest(manifest_path, manifest)

    expected = manifest["files"]["file.bin"]

    # Exercise the read-only restoration path without relying on
    # Windows chmod semantics for the temporary file.
    monkeypatch.setattr(refresh, "is_readonly", lambda path: True)

    readonly_calls = []

    def fake_set_readonly(path, readonly):
        readonly_calls.append((path, readonly))

    monkeypatch.setattr(refresh, "set_readonly", fake_set_readonly)

    result = refresh.refresh_file(
        root=root,
        relative="file.bin",
        expected=expected,
        manifest=manifest,
        manifest_path=manifest_path,
    )

    assert result is True
    assert source.exists()
    assert (source, False) in readonly_calls
    assert (source, True) in readonly_calls

def test_interrupted_in_progress_state_recovers_to_pending(tmp_path):
    root = tmp_path
    source = root / "file.bin"
    source.write_bytes(b"original")

    manifest_path = root / "manifest.json"
    manifest = refresh.create_manifest(root)

    manifest["files"]["file.bin"]["status"] = refresh.STATUS_IN_PROGRESS
    manifest["files"]["file.bin"]["last_error"] = None

    refresh.save_manifest(manifest_path, manifest)

    loaded = refresh.load_manifest(manifest_path)

    refresh.recover_in_progress(
        root,
        loaded,
        manifest_path,
    )

    assert loaded["files"]["file.bin"]["status"] == refresh.STATUS_PENDING

