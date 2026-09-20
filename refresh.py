
from __future__ import annotations
from dataclasses import dataclass, field

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_ATOMIC_REPLACE = os.replace


@dataclass
class ValidationResult:
    # File inventory
    expected_files: int = 0
    present_files: int = 0
    verified_files: int = 0

    missing_files: int = 0
    unexpected_files: int = 0
    size_mismatches: int = 0
    hash_mismatches: int = 0
    validation_errors: int = 0

    # Directory inventory
    expected_directories: int = 0
    present_directories: int = 0
    missing_directories: int = 0
    unexpected_directories: int = 0

    # Temporary artifacts
    temporary_files: int = 0

    # Detailed diagnostics
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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SAFETY_MARGIN_GB = 2
HASH_CHUNK_SIZE = 8 * 1024 * 1024
MAX_RETRIES = 3
RETRY_DELAY = 5

SKIP_DIRS = {
    "$RECYCLE.BIN",
    "System Volume Information",
    "Config.Msi",
}

TEMP_SUFFIX = ".refreshtmp"
MANIFEST_VERSION = 2


# ---------------------------------------------------------------------------
# Status / failure codes
# ---------------------------------------------------------------------------

STATUS_PENDING = "PENDING"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_COMPLETED = "COMPLETED"
STATUS_FAILED = "FAILED"

FAIL_NO_SPACE = "NO_SPACE"
FAIL_PERMISSION = "PERMISSION"
FAIL_SOURCE_CHANGED = "SOURCE_CHANGED"
FAIL_TEMP_VERIFY = "TEMP_VERIFY"
FAIL_METADATA_VERIFY = "METADATA_VERIFY"
FAIL_REPLACE = "REPLACE"
FAIL_MISSING_SOURCE = "MISSING_SOURCE"
FAIL_MANIFEST_MISMATCH = "MANIFEST_MISMATCH"
FAIL_ERROR = "ERROR"


logger = logging.getLogger("refresh")


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configure_logging(log_path: Optional[Path] = None) -> None:
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(console)

    if log_path:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        logger.addHandler(file_handler)


def get_free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / (1024 ** 3)


def get_file_signature(path: Path) -> tuple[int, int]:
    st = path.stat()
    return st.st_size, st.st_mtime_ns


# ---------------------------------------------------------------------------
# Read-only handling
# ---------------------------------------------------------------------------

def is_readonly(path: Path) -> bool:
    return not bool(path.stat().st_mode & stat.S_IWRITE)


def set_readonly(path: Path, readonly: bool) -> None:
    mode = path.stat().st_mode

    if readonly:
        path.chmod(mode & ~stat.S_IWRITE)
    else:
        path.chmod(mode | stat.S_IWRITE)


# ---------------------------------------------------------------------------
# Hash / copy / durability
# ---------------------------------------------------------------------------

def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    # logger.info(f"started computing sha256 for {path}")
    with path.open("rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)

            if not chunk:
                break

            digest.update(chunk)
    # logger.info(f"completed computing sha256 for {path}")
    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    # Windows requires a writable handle for fsync/FlushFileBuffers.
    with path.open("r+b") as f:
        f.flush()
        os.fsync(f.fileno())


def copy_writethrough(source: Path, target: Path) -> None:
    shutil.copy2(source, target)
    fsync_file(target)


def preserve_metadata(source: Path, target: Path) -> None:
    shutil.copystat(source, target, follow_symlinks=False)


# ---------------------------------------------------------------------------
# Temporary file helpers
# ---------------------------------------------------------------------------

def temp_path_for(source: Path) -> Path:
    return source.with_name(source.name + TEMP_SUFFIX)


def remove_temp_file(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Manifest persistence
# ---------------------------------------------------------------------------

def manifest_path_for(root: Path) -> Path:
    return root / "refresh-manifest.json"


def _atomic_json_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )

    temp = Path(temp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())

        _ATOMIC_REPLACE(temp, path)

    finally:
        if temp.exists():
            temp.unlink()


def save_manifest(
    manifest_path: Path,
    manifest: dict[str, Any],
) -> None:
    manifest["updated_at"] = utc_now()
    _atomic_json_write(manifest_path, manifest)


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = json.load(f)

    if manifest.get("format_version") != MANIFEST_VERSION:
        raise ValueError("Unsupported manifest format version")

    return manifest


def validate_manifest_root(
    root: Path,
    manifest: dict[str, Any],
) -> None:
    actual = root.resolve()
    expected = Path(manifest["root"]).resolve()

    if actual != expected:
        raise ValueError(
            f"Manifest belongs to {expected}, not {actual}"
        )


# ---------------------------------------------------------------------------
# Manifest creation
# ---------------------------------------------------------------------------

def relative_key(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def create_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()

    if not root.exists() or not root.is_dir():
        raise ValueError(f"Path is not a directory: {root}")

    files: dict[str, Any] = {}
    directories: list[str] = []

    for current, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)

        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not (current_path / d).is_symlink()
        ]

        if current_path != root:
            directories.append(
                relative_key(root, current_path)
            )

        for name in filenames:
            path = current_path / name

            if path.is_symlink() or not path.is_file():
                continue

            relative = relative_key(root, path)
            stat_result = path.stat()

            files[relative] = {
                "size": stat_result.st_size,
                "mtime_ns": stat_result.st_mtime_ns,
                "sha256": sha256_of(path),
                "status": STATUS_PENDING,
                "attempts": 0,
                "last_error": None,
                "last_error_detail": None,
            }

    return {
        "format_version": MANIFEST_VERSION,
        "root": str(root),
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": STATUS_PENDING,
        "directories": sorted(directories),
        "files": dict(sorted(files.items())),
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_source_against_manifest(
    source: Path,
    expected: dict[str, Any],
) -> tuple[bool, str]:
    """
    Authoritative source validation.

    Size + SHA-256 are required.

    mtime_ns is recorded for diagnostics but is NOT used as an authority
    because some filesystems may legitimately alter timestamps.
    """

    if source.is_symlink() or not source.is_file():
        return False, FAIL_MISSING_SOURCE

    try:
        if source.stat().st_size != expected["size"]:
            return False, FAIL_MANIFEST_MISMATCH

        if sha256_of(source) != expected["sha256"]:
            return False, FAIL_MANIFEST_MISMATCH

        return True, ""

    except OSError:
        return False, FAIL_ERROR


def validate_temp(
    source: Path,
    temp: Path,
    expected: dict[str, Any],
) -> tuple[bool, str]:
    """
    Validate the temporary target before os.replace().
    """

    if not temp.exists() or temp.is_symlink() or not temp.is_file():
        return False, FAIL_TEMP_VERIFY

    try:
        if temp.stat().st_size != expected["size"]:
            return False, FAIL_TEMP_VERIFY

        if sha256_of(temp) != expected["sha256"]:
            return False, FAIL_TEMP_VERIFY

        # Validate metadata relevant to this refresh operation.
        source_stat = source.stat()
        temp_stat = temp.stat()

        if temp_stat.st_mode != source_stat.st_mode:
            return False, FAIL_METADATA_VERIFY

        if temp_stat.st_mtime_ns != source_stat.st_mtime_ns:
            return False, FAIL_METADATA_VERIFY

        return True, ""

    except OSError:
        return False, FAIL_TEMP_VERIFY


# ---------------------------------------------------------------------------
# Manifest task state
# ---------------------------------------------------------------------------

def mark_file(
    manifest: dict[str, Any],
    relative: str,
    status: str,
    error: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    item = manifest["files"][relative]

    item["status"] = status
    item["last_error"] = error
    item["last_error_detail"] = detail


def fail_task(
    manifest: dict[str, Any],
    relative: str,
    reason: str,
    detail: str,
    manifest_path: Path,
) -> bool:
    mark_file(
        manifest,
        relative,
        STATUS_FAILED,
        reason,
        detail,
    )

    save_manifest(manifest_path, manifest)

    return False


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------

def recover_in_progress(
    root: Path,
    manifest: dict[str, Any],
    manifest_path: Path,
) -> bool:
    """
    Conservative recovery after interruption.

    We never automatically trust an old .refreshtmp.

    If the source still exactly matches the manifest:
        delete old temp
        return task to PENDING

    If the source is missing or changed:
        mark FAILED and stop processing.

    This avoids automatically overwriting data that may have changed while
    the program was not running.
    """

    safe = True

    for relative, item in manifest["files"].items():

        if item.get("status") != STATUS_IN_PROGRESS:
            continue

        source = root / Path(relative)
        temp = temp_path_for(source)

        # Old temp is not trusted after interruption.
        remove_temp_file(temp)

        if not source.exists():
            mark_file(
                manifest,
                relative,
                STATUS_FAILED,
                FAIL_MISSING_SOURCE,
                "Source disappeared while task was IN_PROGRESS",
            )

            safe = False
            continue

        matches, reason = validate_source_against_manifest(
            source,
            item,
        )

        if not matches:
            mark_file(
                manifest,
                relative,
                STATUS_FAILED,
                reason or FAIL_MANIFEST_MISMATCH,
                "Source no longer matches original manifest after recovery",
            )

            safe = False
            continue

        mark_file(
            manifest,
            relative,
            STATUS_PENDING,
            None,
            "Recovered after interruption",
        )

    save_manifest(manifest_path, manifest)

    return safe


# ---------------------------------------------------------------------------
# Core refresh operation
# ---------------------------------------------------------------------------


def refresh_file(
    root: Path,
    relative: str,
    expected: dict[str, Any],
    manifest: dict[str, Any],
    manifest_path: Path,
) -> bool:

    source = root / Path(relative)
    temp = temp_path_for(source)

    # ---------------------------------------------------------------
    # Safety check 1: source must exist and be a normal file.
    # ---------------------------------------------------------------

    if source.is_symlink() or not source.is_file():
        return fail_task(
            manifest,
            relative,
            FAIL_MISSING_SOURCE,
            "Source is missing, is a symlink, or is not a regular file",
            manifest_path,
        )

    # ---------------------------------------------------------------
    # Safety check 2: never overwrite an unexpected pre-existing temp.
    # ---------------------------------------------------------------

    if temp.exists() or temp.is_symlink():
        return fail_task(
            manifest,
            relative,
            FAIL_ERROR,
            f"Pre-existing temporary file found: {temp}",
            manifest_path,
        )

    # ---------------------------------------------------------------
    # Safety check 3: source must match manifest BEFORE doing anything.
    # ---------------------------------------------------------------

    source_ok, reason = validate_source_against_manifest(
        source,
        expected,
    )

    if not source_ok:
        return fail_task(
            manifest,
            relative,
            reason or FAIL_MANIFEST_MISMATCH,
            "Source does not match the original manifest; refusing replacement",
            manifest_path,
        )

    # ---------------------------------------------------------------
    # Safety check 4: enough disk space.
    # ---------------------------------------------------------------

    required_bytes = expected["size"]
    safety_bytes = SAFETY_MARGIN_GB * (1024 ** 3)

    if get_free_gb(source) * (1024 ** 3) < required_bytes + safety_bytes:
        return fail_task(
            manifest,
            relative,
            FAIL_NO_SPACE,
            "Insufficient free space for temporary copy and safety margin",
            manifest_path,
        )

    original_readonly = is_readonly(source)

    item = manifest["files"][relative]
    item["attempts"] = int(item.get("attempts", 0)) + 1

    mark_file(
        manifest,
        relative,
        STATUS_IN_PROGRESS,
        None,
        None,
    )

    save_manifest(manifest_path, manifest)

    try:
        for attempt in range(1, MAX_RETRIES + 1):

            try:
                # ---------------------------------------------------
                # Safety check 5:
                # source must still match immediately before copying.
                # ---------------------------------------------------

                source_ok, reason = validate_source_against_manifest(
                    source,
                    expected,
                )

                if not source_ok:
                    return fail_task(
                        manifest,
                        relative,
                        reason or FAIL_SOURCE_CHANGED,
                        "Source changed before temporary copy",
                        manifest_path,
                    )

                # ---------------------------------------------------
                # Create temporary copy.
                # ---------------------------------------------------

                copy_writethrough(source, temp)

                # ---------------------------------------------------
                # Safety check 6:
                # SOURCE MUST STILL MATCH immediately after copying.
                #
                # This check intentionally happens BEFORE validate_temp.
                # If the source changed while it was being copied, the
                # result must be SOURCE_CHANGED, not METADATA_VERIFY.
                # ---------------------------------------------------

                source_ok, _reason = validate_source_against_manifest(
                    source,
                    expected,
                )

                if not source_ok:
                    remove_temp_file(temp)

                    return fail_task(
                        manifest,
                        relative,
                        FAIL_SOURCE_CHANGED,
                        "Source changed during refresh; original retained",
                        manifest_path,
                    )

                # ---------------------------------------------------
                # Safety check 7 + 8:
                # Now validate the temporary copy itself.
                #
                # The source has already been independently confirmed
                # unchanged, so failures here belong to the temp copy.
                # ---------------------------------------------------

                temp_ok, temp_reason = validate_temp(
                    source,
                    temp,
                    expected,
                )

                if not temp_ok:
                    remove_temp_file(temp)

                    return fail_task(
                        manifest,
                        relative,
                        temp_reason or FAIL_TEMP_VERIFY,
                        "Temporary file failed validation; original retained",
                        manifest_path,
                    )

                # ---------------------------------------------------
                # At this exact point ALL pre-replacement validations
                # have passed:
                #
                #   1. source existed and was regular
                #   2. source matched manifest before copy
                #   3. source matched manifest after copy
                #   4. temp passed size/hash/metadata validation
                #
                # Only now may os.replace() be called.
                # ---------------------------------------------------

                if original_readonly:
                    set_readonly(source, False)

                try:
                    os.replace(temp, source)
                except OSError as exc:
                    # Original is retained if replacement fails.
                    # Temp is our artifact and can safely be removed.
                    remove_temp_file(temp)

                    if attempt >= MAX_RETRIES:
                        return fail_task(
                            manifest,
                            relative,
                            FAIL_REPLACE,
                            str(exc),
                            manifest_path,
                        )

                    time.sleep(RETRY_DELAY)
                    continue

                # ---------------------------------------------------
                # os.replace() succeeded.
                # ---------------------------------------------------

                mark_file(
                    manifest,
                    relative,
                    STATUS_COMPLETED,
                    None,
                    None,
                )

                save_manifest(manifest_path, manifest)

                return True

            except PermissionError as exc:
                remove_temp_file(temp)

                if attempt >= MAX_RETRIES:
                    return fail_task(
                        manifest,
                        relative,
                        FAIL_PERMISSION,
                        str(exc),
                        manifest_path,
                    )

                time.sleep(RETRY_DELAY)

            except OSError as exc:
                remove_temp_file(temp)

                if attempt >= MAX_RETRIES:
                    return fail_task(
                        manifest,
                        relative,
                        FAIL_ERROR,
                        str(exc),
                        manifest_path,
                    )

                time.sleep(RETRY_DELAY)

            except Exception as exc:
                remove_temp_file(temp)

                return fail_task(
                    manifest,
                    relative,
                    FAIL_ERROR,
                    repr(exc),
                    manifest_path,
                )

        return False

    finally:
        # NEVER delete source here.
        #
        # Only our temporary artifact may be cleaned up.
        remove_temp_file(temp)

        # Restore readonly state if we changed it.
        if source.exists() and original_readonly:
            try:
                set_readonly(source, True)
            except OSError as exc:
                logger.error(
                    "Could not restore readonly state for %s: %s",
                    source,
                    exc,
                )


# ---------------------------------------------------------------------------
# Whole-archive validation
# ---------------------------------------------------------------------------

def validate_archive(
    root: Path,
    manifest: dict[str, Any],
) -> ValidationResult:
    """
    Independently validate the complete archive against the manifest.

    File counts are reported for diagnostics only.

    A successful validation requires:
        - every expected file exists
        - no unexpected files exist
        - every expected file has the expected size
        - every expected file has the expected SHA-256
        - every expected directory exists
        - no unexpected directories exist
        - no temporary refresh files remain
    """

    result = ValidationResult()

    # ---------------------------------------------------------------
    # Expected inventory
    # ---------------------------------------------------------------

    expected_files = set(manifest["files"])
    expected_dirs = set(manifest.get("directories", []))

    result.expected_files = len(expected_files)
    result.expected_directories = len(expected_dirs)

    # ---------------------------------------------------------------
    # Actual directories and files
    # ---------------------------------------------------------------

    actual_dirs: set[str] = set()
    actual_files: set[str] = set()
    temporary_files: set[str] = set()

    for current, dirnames, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)

        # Do not traverse excluded/system directories or symlinks.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in SKIP_DIRS
            and not (current_path / d).is_symlink()
        ]

        if current_path != root:
            actual_dirs.add(
                relative_key(root, current_path)
            )

        for name in filenames:
            path = current_path / name

            relative = relative_key(root, path)

            # Temporary files are never considered archive files.
            if name.endswith(TEMP_SUFFIX):
                temporary_files.add(relative)
                continue

            # Symlinks and non-regular files are not archive files.
            if path.is_symlink() or not path.is_file():
                continue

            actual_files.add(relative)

    result.present_files = len(actual_files)
    result.present_directories = len(actual_dirs)
    result.temporary_files = len(temporary_files)

    # ---------------------------------------------------------------
    # Directory validation
    # ---------------------------------------------------------------

    missing_dirs = sorted(expected_dirs - actual_dirs)
    unexpected_dirs = sorted(actual_dirs - expected_dirs)

    result.missing_directories = len(missing_dirs)
    result.unexpected_directories = len(unexpected_dirs)

    for directory in missing_dirs:
        result.errors.append(
            f"Missing directory: {directory}"
        )

    for directory in unexpected_dirs:
        result.errors.append(
            f"Unexpected directory: {directory}"
        )

    # ---------------------------------------------------------------
    # File inventory validation
    #
    # This is path-based, not count-based.
    #
    # Therefore:
    #
    #   deleted A + added B
    #
    # is detected even when the file count is unchanged.
    # ---------------------------------------------------------------

    missing_files = sorted(expected_files - actual_files)
    unexpected_files = sorted(actual_files - expected_files)

    result.missing_files = len(missing_files)
    result.unexpected_files = len(unexpected_files)

    for relative in missing_files:
        result.errors.append(
            f"Missing file: {relative}"
        )

    for relative in unexpected_files:
        result.errors.append(
            f"Unexpected file: {relative}"
        )

    # ---------------------------------------------------------------
    # Content validation
    # ---------------------------------------------------------------

    for relative in sorted(expected_files & actual_files):
        expected = manifest["files"][relative]
        path = root / Path(relative)

        try:
            actual_size = path.stat().st_size

            if actual_size != expected["size"]:
                result.size_mismatches += 1

                result.errors.append(
                    f"Size mismatch: {relative} "
                    f"(expected {expected['size']}, "
                    f"actual {actual_size})"
                )

                # Size mismatch already proves the file is invalid.
                # No need to calculate a potentially expensive hash.
                continue

            actual_hash = sha256_of(path)

            if actual_hash != expected["sha256"]:
                result.hash_mismatches += 1

                result.errors.append(
                    f"Hash mismatch: {relative} "
                    f"(expected {expected['sha256']}, "
                    f"actual {actual_hash})"
                )

                continue

            result.verified_files += 1

        except OSError as exc:
            result.validation_errors += 1

            result.errors.append(
                f"Could not validate {relative}: {exc}"
            )

    # ---------------------------------------------------------------
    # Temporary-file validation
    # ---------------------------------------------------------------

    for relative in sorted(temporary_files):
        result.errors.append(
            f"Leftover temporary file: {relative}"
        )

    return result

def get_refresh_summary(
    manifest: dict[str, Any],
) -> dict[str, int]:
    """
    Return counts for refresh task states.
    """

    summary = {
        STATUS_PENDING: 0,
        STATUS_IN_PROGRESS: 0,
        STATUS_COMPLETED: 0,
        STATUS_FAILED: 0,
    }

    for item in manifest["files"].values():
        status = item.get("status")

        if status in summary:
            summary[status] += 1

    return summary

def log_final_summary(
    root: Path,
    manifest: dict[str, Any],
    validation: ValidationResult,
) -> None:
    """
    Log a human-readable final refresh/validation summary.
    """

    refresh = get_refresh_summary(manifest)

    logger.info("")
    logger.info("=" * 70)
    logger.info("REFRESH SUMMARY")
    logger.info("=" * 70)

    logger.info("Root: %s", root)

    logger.info("")
    logger.info("Manifest inventory:")
    logger.info(
        "  Expected files:        %d",
        validation.expected_files,
    )
    logger.info(
        "  Expected directories:  %d",
        validation.expected_directories,
    )

    logger.info("")
    logger.info("Final archive inventory:")
    logger.info(
        "  Files present:         %d",
        validation.present_files,
    )
    logger.info(
        "  Directories present:   %d",
        validation.present_directories,
    )

    logger.info("")
    logger.info("Refresh tasks:")
    logger.info(
        "  Completed:             %d",
        refresh[STATUS_COMPLETED],
    )
    logger.info(
        "  Failed:                %d",
        refresh[STATUS_FAILED],
    )
    logger.info(
        "  Pending:               %d",
        refresh[STATUS_PENDING],
    )
    logger.info(
        "  In progress:           %d",
        refresh[STATUS_IN_PROGRESS],
    )

    logger.info("")
    logger.info("Final validation:")
    logger.info(
        "  Files verified:        %d",
        validation.verified_files,
    )
    logger.info(
        "  Missing files:         %d",
        validation.missing_files,
    )
    logger.info(
        "  Unexpected files:      %d",
        validation.unexpected_files,
    )
    logger.info(
        "  Size mismatches:       %d",
        validation.size_mismatches,
    )
    logger.info(
        "  Hash mismatches:       %d",
        validation.hash_mismatches,
    )
    logger.info(
        "  Validation errors:     %d",
        validation.validation_errors,
    )
    logger.info(
        "  Missing directories:   %d",
        validation.missing_directories,
    )
    logger.info(
        "  Unexpected directories:%d",
        validation.unexpected_directories,
    )
    logger.info(
        "  Temporary files:       %d",
        validation.temporary_files,
    )

    logger.info("")

    if validation.valid:
        logger.info("RESULT: VALIDATION PASSED")
    else:
        logger.error("RESULT: VALIDATION FAILED")

        for error in validation.errors:
            logger.error("  %s", error)

    logger.info("=" * 70)
    logger.info("")

# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def process(
    root: Path,
    manifest_path: Path,
    supplied_manifest: bool,
) -> int:

    root = root.resolve()

    # ---------------------------------------------------------------
    # Manifest selection
    # ---------------------------------------------------------------

    if supplied_manifest and manifest_path.exists():
        logger.info("manifest file supplied and exists")
        manifest = load_manifest(manifest_path)
        validate_manifest_root(root, manifest)

    elif supplied_manifest and not manifest_path.exists():
        logger.info("manifest file supplied but it doesnt exists")
        manifest = create_manifest(root)
        save_manifest(manifest_path, manifest)

    else:

        # No manifest argument = always a fresh run.
        logger.info("manifest file not supplied")
        manifest = create_manifest(root)
        save_manifest(manifest_path, manifest)

    # ---------------------------------------------------------------
    # Recover interrupted tasks.
    # ---------------------------------------------------------------

    if not recover_in_progress(
        root,
        manifest,
        manifest_path,
    ):
        manifest["status"] = STATUS_FAILED
        save_manifest(manifest_path, manifest)
        return 2

    overall_ok = True

    # ---------------------------------------------------------------
    # Process every file.
    # ---------------------------------------------------------------

    for relative, expected in list(
        manifest["files"].items()
    ):
        logger.info(f"started processing {root / Path(relative)}")
        status = expected.get("status")

        if status == STATUS_COMPLETED:

            # Completed means completed according to the manifest.
            # We still independently verify it on restart.
            ok, reason = validate_source_against_manifest(
                root / Path(relative),
                expected,
            )

            if ok:
                continue

            fail_task(
                manifest,
                relative,
                FAIL_MANIFEST_MISMATCH,
                "Previously completed file no longer matches manifest",
                manifest_path,
            )

            overall_ok = False
            break

        if not refresh_file(
            root,
            relative,
            expected,
            manifest,
            manifest_path,
        ):
            overall_ok = False

            # These failures indicate an unsafe/ambiguous archive state.
            # Stop instead of proceeding blindly.
            if expected.get("last_error") in {
                FAIL_SOURCE_CHANGED,
                FAIL_MISSING_SOURCE,
                FAIL_MANIFEST_MISMATCH,
                FAIL_TEMP_VERIFY,
                FAIL_METADATA_VERIFY,
            }:
                break

    # ---------------------------------------------------------------
    # Independent final validation.
    #
    # IMPORTANT:
    #
    # File counts are only reported as statistics.
    # They are NOT used as the validation criterion.
    #
    # The validator compares exact relative paths and then verifies
    # size + SHA-256 for every expected file.
    # ---------------------------------------------------------------

    validation = validate_archive(
        root,
        manifest,
    )

    if not validation.valid:
        overall_ok = False

    # ---------------------------------------------------------------
    # Validate refresh task states as well.
    # ---------------------------------------------------------------

    refresh_summary = get_refresh_summary(manifest)

    if refresh_summary[STATUS_PENDING] > 0:
        overall_ok = False

    if refresh_summary[STATUS_IN_PROGRESS] > 0:
        overall_ok = False

    if refresh_summary[STATUS_FAILED] > 0:
        overall_ok = False

    # ---------------------------------------------------------------
    # Store final validation information in the manifest.
    # ---------------------------------------------------------------

    manifest["validation"] = validation.to_dict()

    manifest["refresh_summary"] = refresh_summary

    manifest["status"] = (
        STATUS_COMPLETED
        if overall_ok
        else STATUS_FAILED
    )

    # Log the complete final summary before saving.
    log_final_summary(
        root,
        manifest,
        validation,
    )

    save_manifest(
        manifest_path,
        manifest,
    )

    return 0 if overall_ok else 2


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Safely refresh archive files using verified "
            "temporary copies."
        )
    )

    parser.add_argument(
        "-p",
        "--path",
        # dest="root",
        type=Path,
        required=True,
        help="Folder to refresh",
    )

    parser.add_argument(
        "--log",
        type=Path,
        default=None,
    )

    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
    )

    return parser


def main(
    argv: Optional[list[str]] = None,
) -> int:
    logger.info("refresh started")
    parser = build_parser()
    args = parser.parse_args(argv)

    configure_logging(args.log)

    root = args.path

    manifest_path = (
        args.manifest
        if args.manifest is not None
        else Path.cwd() / "manifest.json"
    )
    logger.info(f"manifest file: {manifest_path}")

    try:
        logger.info("process started")
        return process(
            root,
            manifest_path,
            supplied_manifest=args.manifest is not None,
        )
        

    except Exception:
        logger.exception("Fatal error")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

