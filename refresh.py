from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import stat
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ============================================================
# Configuration
# ============================================================

SAFETY_MARGIN_GB = 2
HASH_CHUNK_SIZE = 128 * 1024 * 1024

MAX_RETRIES = 3
RETRY_DELAY = 5

# Save manifest periodically rather than after every file.
# This is important for HDD performance.
MANIFEST_CHECKPOINT_FILES = 50

SKIP_DIRS = {
    "$RECYCLE.BIN",
    "System Volume Information",
    "Config.Msi",
}

TEMP_SUFFIX = ".refreshtmp"

MANIFEST_VERSION = 2

DEFAULT_MANIFEST_NAME = "refresh_manifest.json"
DEFAULT_LOG_NAME = "refresh.log"


# ============================================================
# Status / failure constants
# ============================================================

PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
COMPLETED = "COMPLETED"
FAILED = "FAILED"

NO_SPACE = "NO_SPACE"
PERMISSION = "PERMISSION"
SOURCE_CHANGED = "SOURCE_CHANGED"
TEMP_VERIFY = "TEMP_VERIFY"
METADATA_VERIFY = "METADATA_VERIFY"
REPLACE = "REPLACE"
MISSING_SOURCE = "MISSING_SOURCE"
MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
ERROR = "ERROR"


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger("refresh")


def setup_logging(log_path: Path) -> None:
    """
    Configure console + file logging.

    The log file is outside the input directory by default because
    it is created relative to the current working directory.
    """

    log_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger.setLevel(logging.INFO)

    # Avoid duplicate handlers if setup_logging() is called again.
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s"
    )

    file_handler = logging.FileHandler(
        log_path,
        encoding="utf-8",
    )

    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# ============================================================
# Validation result
# ============================================================

@dataclass
class ValidationResult:
    expected_files: int = 0
    present_files: int = 0
    verified_files: int = 0

    missing_files: int = 0
    unexpected_files: int = 0

    size_mismatches: int = 0
    hash_mismatches: int = 0

    validation_errors: int = 0

    expected_directories: int = 0
    present_directories: int = 0
    missing_directories: int = 0
    unexpected_directories: int = 0

    temporary_files: int = 0

    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_files": self.expected_files,
            "present_files": self.present_files,
            "verified_files": self.verified_files,
            "missing_files": self.missing_files,
            "unexpected_files": self.unexpected_files,
            "size_mismatches": self.size_mismatches,
            "hash_mismatches": self.hash_mismatches,
            "validation_errors": self.validation_errors,
            "expected_directories": self.expected_directories,
            "present_directories": self.present_directories,
            "missing_directories": self.missing_directories,
            "unexpected_directories": self.unexpected_directories,
            "temporary_files": self.temporary_files,
            "valid": self.valid,
            "errors": list(self.errors),
        }


# ============================================================
# Basic helpers
# ============================================================

def is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def relative_path(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


# ============================================================
# Hashing
# ============================================================

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


# ============================================================
# File durability
# ============================================================

def fsync_file(path: Path) -> None:
    with path.open("r+b") as f:
        f.flush()
        os.fsync(f.fileno())


def copy_writethrough(
    source: Path,
    target: Path,
) -> None:
    """
    copy2() preserves normal metadata and may use platform
    optimized copy mechanisms.

    fsync() ensures the copied file is flushed before validation.
    """

    shutil.copy2(
        source,
        target,
    )

    fsync_file(target)


# ============================================================
# Manifest
# ============================================================

def save_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    """
    Atomically write the manifest.

    The manifest lives outside the input tree by default.
    """

    manifest_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fd, tmp_name = tempfile.mkstemp(
        prefix=manifest_path.name + ".",
        suffix=".tmp",
        dir=manifest_path.parent,
        text=True,
    )

    tmp_path = Path(tmp_name)

    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as f:

            json.dump(
                manifest,
                f,
                indent=2,
                ensure_ascii=False,
            )

            f.flush()
            os.fsync(f.fileno())

        os.replace(
            tmp_path,
            manifest_path,
        )

    finally:

        try:
            tmp_path.unlink(
                missing_ok=True
            )
        except OSError:
            pass


def load_manifest(
    manifest_path: Path,
) -> dict[str, Any]:

    with manifest_path.open(
        "r",
        encoding="utf-8",
    ) as f:
        manifest = json.load(f)

    if manifest.get("version") != MANIFEST_VERSION:
        raise ValueError(
            f"Unsupported manifest version: "
            f"{manifest.get('version')!r}"
        )

    if "files" not in manifest:
        raise ValueError(
            "Manifest is missing 'files'."
        )

    if "directories" not in manifest:
        raise ValueError(
            "Manifest is missing 'directories'."
        )

    return manifest


# ============================================================
# Manifest creation
# ============================================================

def create_manifest(
    root: Path,
) -> dict[str, Any]:

    root = root.resolve()

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "root": str(root),
        "created_at": time.time(),
        "files": {},
        "directories": {},
    }

    logger.info(
        "Creating manifest for input: %s",
        root,
    )

    file_count = 0
    total_bytes = 0

    for current_root, dirs, files in os.walk(root):

        current_root_path = Path(
            current_root
        )

        # Do not descend into system directories.
        dirs[:] = [
            d
            for d in dirs
            if d not in SKIP_DIRS
        ]

        # Record directories.
        for dirname in dirs:

            directory = (
                current_root_path / dirname
            )

            if directory.is_symlink():
                continue

            rel = relative_path(
                root,
                directory,
            )

            manifest["directories"][rel] = {}

        for filename in files:

            path = (
                current_root_path / filename
            )

            if path.is_symlink():
                continue

            if filename.endswith(
                TEMP_SUFFIX
            ):
                continue

            if not is_regular_file(path):
                continue

            rel = relative_path(
                root,
                path,
            )

            try:

                st = path.stat()

                logger.info(
                    "Hashing [%d] %s",
                    file_count + 1,
                    rel,
                )

                digest = sha256_of(path)

                manifest["files"][rel] = {
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                    "sha256": digest,
                    "status": PENDING,
                    "attempts": 0,
                    "errors": [],
                }

                file_count += 1
                total_bytes += st.st_size

            except OSError as exc:

                raise RuntimeError(
                    f"Unable to inventory {path}: {exc}"
                ) from exc

    manifest["summary"] = {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "directory_count": len(
            manifest["directories"]
        ),
    }

    logger.info(
        "Manifest created: %d files, %.2f GB",
        file_count,
        total_bytes / (1024 ** 3),
    )

    return manifest


# ============================================================
# Source validation
# ============================================================

def validate_source_against_manifest(
    source: Path,
    expected: dict[str, Any],
) -> tuple[bool, str | None]:
    """
    One full SHA-256 verification of the source.

    IMPORTANT ASSUMPTION:
        The input tree is not modified after the process starts.

    Therefore we do not hash the source a second time after copying.
    """

    if not is_regular_file(source):
        return (
            False,
            "source is missing or is not a regular file",
        )

    try:

        st = source.stat()

        expected_size = expected["size"]

        if st.st_size != expected_size:

            return (
                False,
                f"size mismatch: "
                f"expected={expected_size}, "
                f"actual={st.st_size}",
            )

        actual_hash = sha256_of(source)

        if actual_hash != expected["sha256"]:

            return (
                False,
                f"SHA-256 mismatch: "
                f"expected={expected['sha256']}, "
                f"actual={actual_hash}",
            )

        return True, None

    except OSError as exc:

        return (
            False,
            f"unable to validate source: {exc}",
        )


# ============================================================
# Temp validation
# ============================================================

def validate_temp(
    source: Path,
    temp: Path,
    expected: dict[str, Any],
) -> tuple[bool, str | None]:

    if not is_regular_file(temp):
        return (
            False,
            "temporary file is missing or invalid",
        )

    try:

        source_stat = source.stat()
        temp_stat = temp.stat()

        expected_size = expected["size"]
        expected_hash = expected["sha256"]

        # Size first — avoid hashing if the size is already wrong.
        if temp_stat.st_size != expected_size:

            return (
                False,
                f"temporary size mismatch: "
                f"expected={expected_size}, "
                f"actual={temp_stat.st_size}",
            )

        actual_hash = sha256_of(temp)

        if actual_hash != expected_hash:

            return (
                False,
                f"temporary SHA-256 mismatch: "
                f"expected={expected_hash}, "
                f"actual={actual_hash}",
            )

        source_mode = stat.S_IMODE(
            source_stat.st_mode
        )

        temp_mode = stat.S_IMODE(
            temp_stat.st_mode
        )

        if source_mode != temp_mode:

            return (
                False,
                f"mode mismatch: "
                f"source={oct(source_mode)}, "
                f"temp={oct(temp_mode)}",
            )

        if (
            temp_stat.st_mtime_ns
            != source_stat.st_mtime_ns
        ):

            return (
                False,
                f"mtime mismatch: "
                f"source={source_stat.st_mtime_ns}, "
                f"temp={temp_stat.st_mtime_ns}",
            )

        return True, None

    except OSError as exc:

        return (
            False,
            f"temporary validation failed: {exc}",
        )


# ============================================================
# Readonly handling
# ============================================================

def ensure_writable(
    path: Path,
) -> int:

    st = path.stat()

    original_mode = stat.S_IMODE(
        st.st_mode
    )

    if not (
        original_mode & stat.S_IWUSR
    ):

        new_mode = (
            original_mode
            | stat.S_IWUSR
        )

        os.chmod(
            path,
            new_mode,
        )

    return original_mode


def restore_mode(
    path: Path,
    original_mode: int,
) -> None:

    try:

        os.chmod(
            path,
            original_mode,
        )

    except OSError:

        logger.warning(
            "Could not restore permissions for %s",
            path,
        )


# ============================================================
# Temporary file cleanup
# ============================================================

def remove_temp(
    temp: Path,
) -> None:

    try:

        temp.unlink(
            missing_ok=True
        )

    except OSError as exc:

        logger.warning(
            "Could not remove temporary file %s: %s",
            temp,
            exc,
        )


# ============================================================
# Failure handling
# ============================================================

def record_failure(
    item: dict[str, Any],
    code: str,
    message: str,
) -> None:

    item["status"] = FAILED

    item.setdefault(
        "errors",
        [],
    ).append(
        {
            "code": code,
            "message": message,
            "time": time.time(),
        }
    )


# ============================================================
# Recovery
# ============================================================

def recover_in_progress(
    root: Path,
    manifest: dict[str, Any],
) -> bool:

    changed = False

    for rel, item in manifest["files"].items():

        if item.get("status") != IN_PROGRESS:
            continue

        source = (
            root / Path(rel)
        )

        temp = source.with_name(
            source.name + TEMP_SUFFIX
        )

        logger.warning(
            "Recovering interrupted file: %s",
            rel,
        )

        remove_temp(temp)

        valid, error = (
            validate_source_against_manifest(
                source,
                item,
            )
        )

        if valid:

            item["status"] = PENDING
            changed = True

        else:

            record_failure(
                item,
                SOURCE_CHANGED,
                error
                or "source failed recovery validation",
            )

            changed = True

    return changed


# ============================================================
# Refresh one file
# ============================================================

def refresh_file(
    root: Path,
    rel: str,
    item: dict[str, Any],
) -> bool:
    """
    Refresh one file.

    ASSUMPTION:
        No file under root is modified externally after
        this process starts.

    Workflow:

        source SHA-256
             ↓
        copy2 → temp
             ↓
        fsync temp
             ↓
        temp SHA-256
             ↓
        metadata verification
             ↓
        atomic replace
    """

    source = root / Path(rel)

    temp = source.with_name(
        source.name + TEMP_SUFFIX
    )

    logger.info(
        "Refreshing: %s",
        rel,
    )

    # --------------------------------------------------------
    # Source must exist and be a regular file.
    # --------------------------------------------------------

    if not is_regular_file(source):

        record_failure(
            item,
            MISSING_SOURCE,
            "source is missing or is not a regular file",
        )

        return False

    # --------------------------------------------------------
    # Remove stale temp.
    # --------------------------------------------------------

    if temp.exists():

        logger.warning(
            "Removing stale temp file: %s",
            temp,
        )

        remove_temp(temp)

    # --------------------------------------------------------
    # Verify source ONCE.
    # --------------------------------------------------------

    valid, error = (
        validate_source_against_manifest(
            source,
            item,
        )
    )

    if not valid:

        record_failure(
            item,
            SOURCE_CHANGED,
            error or "source validation failed",
        )

        return False

    # --------------------------------------------------------
    # Space check.
    # --------------------------------------------------------

    try:

        required = item["size"]

        free = shutil.disk_usage(
            source.parent
        ).free

        safety_margin = (
            SAFETY_MARGIN_GB
            * 1024 ** 3
        )

        if free < (
            required
            + safety_margin
        ):

            record_failure(
                item,
                NO_SPACE,
                f"insufficient free space: "
                f"required={required}, "
                f"free={free}, "
                f"margin={safety_margin}",
            )

            return False

    except OSError as exc:

        record_failure(
            item,
            ERROR,
            f"unable to check disk space: {exc}",
        )

        return False

    # --------------------------------------------------------
    # Mark IN_PROGRESS in memory.
    #
    # It is checkpointed periodically by process().
    # --------------------------------------------------------

    item["status"] = IN_PROGRESS

    original_mode: int | None = None

    try:

        for attempt in range(
            item.get("attempts", 0) + 1,
            MAX_RETRIES + 1,
        ):

            item["attempts"] = attempt

            logger.info(
                "Attempt %d/%d: %s",
                attempt,
                MAX_RETRIES,
                rel,
            )

            remove_temp(temp)

            try:

                # --------------------------------------------
                # Make source writable if required.
                # --------------------------------------------

                original_mode = ensure_writable(
                    source
                )

                # --------------------------------------------
                # Copy.
                # --------------------------------------------

                logger.info(
                    "Copying to temporary file: %s",
                    rel,
                )

                copy_writethrough(
                    source,
                    temp,
                )

                # --------------------------------------------
                # Verify temp.
                # --------------------------------------------

                logger.info(
                    "Verifying temporary file: %s",
                    rel,
                )

                valid, error = validate_temp(
                    source,
                    temp,
                    item,
                )

                if not valid:

                    logger.error(
                        "Temp verification failed for %s: %s",
                        rel,
                        error,
                    )

                    if attempt < MAX_RETRIES:

                        time.sleep(
                            RETRY_DELAY
                        )

                        continue

                    record_failure(
                        item,
                        TEMP_VERIFY,
                        error
                        or "temporary file verification failed",
                    )

                    return False

                # --------------------------------------------
                # Atomic replacement.
                # --------------------------------------------

                logger.info(
                    "Replacing original: %s",
                    rel,
                )

                try:

                    os.replace(
                        temp,
                        source,
                    )

                except PermissionError as exc:

                    if attempt < MAX_RETRIES:

                        logger.warning(
                            "Replace permission error for %s: %s",
                            rel,
                            exc,
                        )

                        time.sleep(
                            RETRY_DELAY
                        )

                        continue

                    record_failure(
                        item,
                        REPLACE,
                        f"replace failed: {exc}",
                    )

                    return False

                except OSError as exc:

                    if attempt < MAX_RETRIES:

                        logger.warning(
                            "Replace failed for %s: %s",
                            rel,
                            exc,
                        )

                        time.sleep(
                            RETRY_DELAY
                        )

                        continue

                    record_failure(
                        item,
                        REPLACE,
                        f"replace failed: {exc}",
                    )

                    return False

                # --------------------------------------------
                # Success.
                # --------------------------------------------

                item["status"] = COMPLETED
                item["errors"] = []

                logger.info(
                    "Completed: %s",
                    rel,
                )

                return True

            except PermissionError as exc:

                if attempt < MAX_RETRIES:

                    logger.warning(
                        "Permission error for %s: %s",
                        rel,
                        exc,
                    )

                    time.sleep(
                        RETRY_DELAY
                    )

                    continue

                record_failure(
                    item,
                    PERMISSION,
                    str(exc),
                )

                return False

            except OSError as exc:

                if attempt < MAX_RETRIES:

                    logger.warning(
                        "I/O error for %s: %s",
                        rel,
                        exc,
                    )

                    time.sleep(
                        RETRY_DELAY
                    )

                    continue

                record_failure(
                    item,
                    ERROR,
                    str(exc),
                )

                return False

            except Exception as exc:

                record_failure(
                    item,
                    ERROR,
                    f"{type(exc).__name__}: {exc}",
                )

                return False

    finally:

        remove_temp(temp)

        if (
            original_mode is not None
            and source.exists()
        ):

            restore_mode(
                source,
                original_mode,
            )

    return False


# ============================================================
# Final archive validation
# ============================================================

def validate_archive(
    root: Path,
    manifest: dict[str, Any],
) -> ValidationResult:

    result = ValidationResult()

    expected_files = set(
        manifest["files"].keys()
    )

    expected_dirs = set(
        manifest["directories"].keys()
    )

    result.expected_files = len(
        expected_files
    )

    result.expected_directories = len(
        expected_dirs
    )

    actual_files: set[str] = set()
    actual_dirs: set[str] = set()

    # --------------------------------------------------------
    # Inventory actual archive.
    # --------------------------------------------------------

    for current_root, dirs, files in os.walk(root):

        current_root_path = Path(
            current_root
        )

        dirs[:] = [
            d
            for d in dirs
            if d not in SKIP_DIRS
        ]

        for dirname in dirs:

            directory = (
                current_root_path / dirname
            )

            if directory.is_symlink():
                continue

            rel = relative_path(
                root,
                directory,
            )

            actual_dirs.add(rel)

        for filename in files:

            path = (
                current_root_path / filename
            )

            if path.is_symlink():
                continue

            rel = relative_path(
                root,
                path,
            )

            if filename.endswith(
                TEMP_SUFFIX
            ):

                result.temporary_files += 1

                result.errors.append(
                    f"temporary file remains: {rel}"
                )

                continue

            if not is_regular_file(path):
                continue

            actual_files.add(rel)

    result.present_files = len(
        actual_files
    )

    result.present_directories = len(
        actual_dirs
    )

    # --------------------------------------------------------
    # Exact file path comparison.
    # --------------------------------------------------------

    missing_files = (
        expected_files - actual_files
    )

    unexpected_files = (
        actual_files - expected_files
    )

    result.missing_files = len(
        missing_files
    )

    result.unexpected_files = len(
        unexpected_files
    )

    for rel in sorted(
        missing_files
    ):

        result.errors.append(
            f"missing file: {rel}"
        )

    for rel in sorted(
        unexpected_files
    ):

        result.errors.append(
            f"unexpected file: {rel}"
        )

    # --------------------------------------------------------
    # Exact directory path comparison.
    # --------------------------------------------------------

    missing_dirs = (
        expected_dirs - actual_dirs
    )

    unexpected_dirs = (
        actual_dirs - expected_dirs
    )

    result.missing_directories = len(
        missing_dirs
    )

    result.unexpected_directories = len(
        unexpected_dirs
    )

    for rel in sorted(
        missing_dirs
    ):

        result.errors.append(
            f"missing directory: {rel}"
        )

    for rel in sorted(
        unexpected_dirs
    ):

        result.errors.append(
            f"unexpected directory: {rel}"
        )

    # --------------------------------------------------------
    # Verify content of expected files.
    # --------------------------------------------------------

    for rel in sorted(
        expected_files & actual_files
    ):

        path = root / Path(rel)

        expected = manifest[
            "files"
        ][rel]

        try:

            st = path.stat()

            if st.st_size != expected["size"]:

                result.size_mismatches += 1
                result.validation_errors += 1

                result.errors.append(
                    f"size mismatch: {rel} "
                    f"(expected {expected['size']}, "
                    f"actual {st.st_size})"
                )

                continue

            actual_hash = sha256_of(
                path
            )

            if actual_hash != expected["sha256"]:

                result.hash_mismatches += 1
                result.validation_errors += 1

                result.errors.append(
                    f"hash mismatch: {rel} "
                    f"(expected {expected['sha256']}, "
                    f"actual {actual_hash})"
                )

                continue

            result.verified_files += 1

        except OSError as exc:

            result.validation_errors += 1

            result.errors.append(
                f"validation error: {rel}: {exc}"
            )

    return result


# ============================================================
# Refresh summary
# ============================================================

def get_refresh_summary(
    manifest: dict[str, Any],
) -> dict[str, int]:

    summary = {
        PENDING: 0,
        IN_PROGRESS: 0,
        COMPLETED: 0,
        FAILED: 0,
    }

    for item in manifest["files"].values():

        status = item.get("status")

        if status in summary:
            summary[status] += 1

    return summary


# ============================================================
# Final summary
# ============================================================

def log_final_summary(
    root: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    validation: ValidationResult,
    refresh_summary: dict[str, int],
    overall_ok: bool,
) -> None:

    print()
    print("=" * 72)
    print("FINAL REFRESH SUMMARY")
    print("=" * 72)

    print(
        f"Input root:        {root}"
    )

    print(
        f"Manifest:          {manifest_path}"
    )

    print()

    print("Manifest:")
    print(
        f"  Expected files:       "
        f"{validation.expected_files}"
    )

    print(
        f"  Expected directories: "
        f"{validation.expected_directories}"
    )

    print()

    print("Archive:")
    print(
        f"  Present files:        "
        f"{validation.present_files}"
    )

    print(
        f"  Verified files:       "
        f"{validation.verified_files}"
    )

    print(
        f"  Present directories:  "
        f"{validation.present_directories}"
    )

    print()

    print("File differences:")
    print(
        f"  Missing files:        "
        f"{validation.missing_files}"
    )

    print(
        f"  Unexpected files:     "
        f"{validation.unexpected_files}"
    )

    print(
        f"  Size mismatches:      "
        f"{validation.size_mismatches}"
    )

    print(
        f"  Hash mismatches:      "
        f"{validation.hash_mismatches}"
    )

    print(
        f"  Validation errors:    "
        f"{validation.validation_errors}"
    )

    print(
        f"  Temporary files:      "
        f"{validation.temporary_files}"
    )

    print()

    print("Directory differences:")
    print(
        f"  Missing directories:  "
        f"{validation.missing_directories}"
    )

    print(
        f"  Unexpected dirs:      "
        f"{validation.unexpected_directories}"
    )

    print()

    print("Task states:")

    for status in (
        PENDING,
        IN_PROGRESS,
        COMPLETED,
        FAILED,
    ):

        print(
            f"  {status:12}: "
            f"{refresh_summary[status]}"
        )

    print()

    print(
        "Archive validation: "
        f"{'PASS' if validation.valid else 'FAIL'}"
    )

    print(
        "Overall result:     "
        f"{'PASS' if overall_ok else 'FAIL'}"
    )

    if validation.errors:

        print()
        print("Validation errors:")

        for error in validation.errors:
            print(
                f"  - {error}"
            )

    print("=" * 72)


# ============================================================
# Process ONE input root
# ============================================================

def process(
    root: Path,
    manifest_path: Path,
    create_new_manifest: bool = False,
) -> bool:

    root = root.resolve()
    manifest_path = manifest_path.resolve()

    # --------------------------------------------------------
    # Input validation.
    # --------------------------------------------------------

    if not root.exists():

        logger.error(
            "Input path does not exist: %s",
            root,
        )

        return False

    if not root.is_dir():

        logger.error(
            "Input path is not a directory: %s",
            root,
        )

        return False

    # --------------------------------------------------------
    # Important safety check:
    #
    # We strongly recommend manifest NOT be inside root.
    # --------------------------------------------------------

    try:

        manifest_path.relative_to(root)

        manifest_inside_root = True

    except ValueError:

        manifest_inside_root = False

    if manifest_inside_root:

        logger.error(
            "Manifest must not be inside the input root."
        )

        logger.error(
            "Input root: %s",
            root,
        )

        logger.error(
            "Manifest: %s",
            manifest_path,
        )

        return False

    # --------------------------------------------------------
    # Start.
    # --------------------------------------------------------

    logger.info("=" * 72)

    logger.info(
        "Starting refresh"
    )

    logger.info(
        "Input root: %s",
        root,
    )

    logger.info(
        "Manifest: %s",
        manifest_path,
    )

    logger.info(
        "IMPORTANT: input files must not be modified "
        "during this process."
    )

    logger.info("=" * 72)

    # --------------------------------------------------------
    # Load or create manifest.
    # --------------------------------------------------------

    if (
        manifest_path.exists()
        and not create_new_manifest
    ):

        logger.info(
            "Loading existing manifest: %s",
            manifest_path,
        )

        manifest = load_manifest(
            manifest_path
        )

        manifest_root = Path(
            manifest["root"]
        ).resolve()

        if manifest_root != root:

            raise RuntimeError(
                "Manifest root mismatch: "
                f"manifest={manifest_root}, "
                f"requested={root}"
            )

    else:

        logger.info(
            "Creating new manifest: %s",
            manifest_path,
        )

        manifest = create_manifest(
            root
        )

        save_manifest(
            manifest_path,
            manifest,
        )

        logger.info(
            "Manifest saved: %s",
            manifest_path,
        )

    # --------------------------------------------------------
    # Recover interrupted work.
    # --------------------------------------------------------

    if recover_in_progress(
        root,
        manifest,
    ):

        logger.info(
            "Saving recovery state..."
        )

        save_manifest(
            manifest_path,
            manifest,
        )

    # --------------------------------------------------------
    # Process files.
    # --------------------------------------------------------

    files = manifest["files"]

    total = len(files)

    checkpoint_counter = 0

    overall_ok = True

    for index, (rel, item) in enumerate(
        files.items(),
        start=1,
    ):

        status = item.get(
            "status"
        )

        if status == COMPLETED:
            continue

        if status == FAILED:

            overall_ok = False
            continue

        logger.info(
            "[%d/%d] %s",
            index,
            total,
            rel,
        )

        success = refresh_file(
            root,
            rel,
            item,
        )

        checkpoint_counter += 1

        if not success:

            overall_ok = False

            # Failure is persisted immediately.
            save_manifest(
                manifest_path,
                manifest,
            )

            checkpoint_counter = 0

        elif (
            checkpoint_counter
            >= MANIFEST_CHECKPOINT_FILES
        ):

            logger.info(
                "Saving manifest checkpoint..."
            )

            save_manifest(
                manifest_path,
                manifest,
            )

            checkpoint_counter = 0

    # --------------------------------------------------------
    # Always save before final validation.
    # --------------------------------------------------------

    save_manifest(
        manifest_path,
        manifest,
    )

    # --------------------------------------------------------
    # Final independent validation.
    # --------------------------------------------------------

    logger.info(
        "Starting final independent archive validation..."
    )

    validation = validate_archive(
        root,
        manifest,
    )

    # --------------------------------------------------------
    # Task-state validation.
    # --------------------------------------------------------

    refresh_summary = (
        get_refresh_summary(
            manifest
        )
    )

    if refresh_summary[PENDING] > 0:
        overall_ok = False

    if refresh_summary[IN_PROGRESS] > 0:
        overall_ok = False

    if refresh_summary[FAILED] > 0:
        overall_ok = False

    if not validation.valid:
        overall_ok = False

    # --------------------------------------------------------
    # Save final result.
    # --------------------------------------------------------

    manifest["validation"] = (
        validation.to_dict()
    )

    manifest["refresh_summary"] = (
        refresh_summary
    )

    manifest["overall_status"] = (
        "COMPLETED"
        if overall_ok
        else "FAILED"
    )

    manifest["completed_at"] = time.time()

    save_manifest(
        manifest_path,
        manifest,
    )

    # --------------------------------------------------------
    # Report.
    # --------------------------------------------------------

    log_final_summary(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        validation=validation,
        refresh_summary=refresh_summary,
        overall_ok=overall_ok,
    )

    return overall_ok


# ============================================================
# CLI
# ============================================================

def parse_args() -> argparse.Namespace:

    parser = argparse.ArgumentParser(
        description=(
            "Safely refresh files under one input directory "
            "using SHA-256 verification, temporary copies "
            "and atomic replacement."
        )
    )

    # Exactly ONE root.
    parser.add_argument(
        "root",
        type=Path,
        help="Input directory to process.",
    )

    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help=(
            "Manifest file path. If omitted, "
            "refresh_manifest.json is created in "
            "the current working directory."
        ),
    )

    parser.add_argument(
        "--new-manifest",
        action="store_true",
        help=(
            "Create a new manifest even if the specified "
            "manifest already exists."
        ),
    )

    parser.add_argument(
        "--checkpoint",
        type=int,
        default=MANIFEST_CHECKPOINT_FILES,
        help=(
            "Save manifest every N processed files. "
            f"Default: {MANIFEST_CHECKPOINT_FILES}"
        ),
    )

    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help=(
            "Log file path. If omitted, "
            "refresh.log is created in the current "
            "working directory."
        ),
    )

    return parser.parse_args()


# ============================================================
# Main
# ============================================================

def main() -> int:

    args = parse_args()

    # --------------------------------------------------------
    # Checkpoint configuration.
    # --------------------------------------------------------

    if args.checkpoint < 1:

        print(
            "--checkpoint must be >= 1",
            file=sys.stderr,
        )

        return 2

    global MANIFEST_CHECKPOINT_FILES

    MANIFEST_CHECKPOINT_FILES = (
        args.checkpoint
    )

    # --------------------------------------------------------
    # Current working directory.
    #
    # This is where default manifest/log files live.
    # --------------------------------------------------------

    working_dir = Path.cwd()

    # --------------------------------------------------------
    # Manifest location.
    # --------------------------------------------------------

    if args.manifest is None:

        manifest_path = (
            working_dir
            / DEFAULT_MANIFEST_NAME
        )

    else:

        manifest_path = args.manifest

    # --------------------------------------------------------
    # Log location.
    # --------------------------------------------------------

    if args.log is None:

        log_path = (
            working_dir
            / DEFAULT_LOG_NAME
        )

    else:

        log_path = args.log

    # --------------------------------------------------------
    # Configure logging BEFORE processing.
    # --------------------------------------------------------

    setup_logging(
        log_path
    )

    logger.info(
        "Log file: %s",
        log_path.resolve(),
    )

    logger.info(
        "Manifest file: %s",
        manifest_path.resolve(),
    )

    logger.info(
        "Working directory: %s",
        working_dir.resolve(),
    )

    # --------------------------------------------------------
    # Process exactly ONE root.
    # --------------------------------------------------------

    try:

        success = process(
            root=args.root,
            manifest_path=manifest_path,
            create_new_manifest=args.new_manifest,
        )

        return 0 if success else 1

    except KeyboardInterrupt:

        logger.error(
            "Process interrupted by user."
        )

        return 1

    except Exception as exc:

        logger.exception(
            "Fatal error: %s",
            exc,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(
        main()
    )