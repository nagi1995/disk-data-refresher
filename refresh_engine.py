# refresh_engine.py

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any


# ============================================================
# Configuration
# ============================================================

HASH_CHUNK_SIZE = 128 * 1024 * 1024

SAFETY_MARGIN_GB = 1
SAFETY_MARGIN_BYTES = (
    SAFETY_MARGIN_GB
    * 1024
    * 1024
    * 1024
)

MAX_RETRIES = 3
RETRY_DELAY = 5

TEMP_SUFFIX = ".refreshtmp"


# ============================================================
# Status constants
# ============================================================

PENDING = "PENDING"
IN_PROGRESS = "IN_PROGRESS"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


# ============================================================
# Failure constants
# ============================================================

NO_SPACE = "NO_SPACE"
PERMISSION = "PERMISSION"
SOURCE_CHANGED = "SOURCE_CHANGED"
TEMP_VERIFY = "TEMP_VERIFY"
METADATA_VERIFY = "METADATA_VERIFY"
REPLACE = "REPLACE"
MISSING_SOURCE = "MISSING_SOURCE"
ERROR = "ERROR"


# ============================================================
# Exceptions
# ============================================================


class RefreshError(RuntimeError):
    """Base exception for refresh failures."""


# ============================================================
# Basic helpers
# ============================================================


def is_regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(HASH_CHUNK_SIZE)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def fsync_file(path: Path) -> None:
    with path.open("r+b") as f:
        f.flush()
        os.fsync(f.fileno())


# ============================================================
# Copy
# ============================================================


def copy_writethrough(
    source: Path,
    target: Path,
) -> None:
    """
    Copy source to target while preserving normal metadata.

    The target is fsync'ed before returning.
    """

    shutil.copy2(
        source,
        target,
    )

    fsync_file(target)


# ============================================================
# Source validation
# ============================================================


def validate_source(
    source: Path,
    expected_size: int,
    expected_mtime_ns: int,
) -> tuple[bool, str | None]:

    if not is_regular_file(source):
        return (
            False,
            "source is missing or is not a regular file",
        )

    try:
        st = source.stat()

        if st.st_size != expected_size:
            return (
                False,
                (
                    "source size changed: "
                    f"expected={expected_size}, "
                    f"actual={st.st_size}"
                ),
            )

        if st.st_mtime_ns != expected_mtime_ns:
            return (
                False,
                (
                    "source mtime changed: "
                    f"expected={expected_mtime_ns}, "
                    f"actual={st.st_mtime_ns}"
                ),
            )

        actual_hash = sha256_of(source)

        return (
            True,
            actual_hash,
        )

    except OSError as exc:
        return (
            False,
            f"unable to validate source: {exc}",
        )


# ============================================================
# Temporary file validation
# ============================================================


def validate_temp(
    source: Path,
    temp: Path,
    expected_size: int,
    expected_hash: str,
) -> tuple[bool, str | None]:

    if not is_regular_file(temp):
        return (
            False,
            "temporary file is missing or invalid",
        )

    try:
        source_stat = source.stat()
        temp_stat = temp.stat()

        # ----------------------------------------------------
        # Size
        # ----------------------------------------------------

        if temp_stat.st_size != expected_size:
            return (
                False,
                (
                    "temporary size mismatch: "
                    f"expected={expected_size}, "
                    f"actual={temp_stat.st_size}"
                ),
            )

        # ----------------------------------------------------
        # Hash
        # ----------------------------------------------------

        actual_hash = sha256_of(temp)

        if actual_hash != expected_hash:
            return (
                False,
                (
                    "temporary SHA-256 mismatch: "
                    f"expected={expected_hash}, "
                    f"actual={actual_hash}"
                ),
            )

        # ----------------------------------------------------
        # Permissions
        # ----------------------------------------------------

        source_mode = stat.S_IMODE(
            source_stat.st_mode
        )

        temp_mode = stat.S_IMODE(
            temp_stat.st_mode
        )

        if os.name == "nt":
            source_writable = bool(source_mode & stat.S_IWRITE)
            temp_writable = bool(temp_mode & stat.S_IWRITE)

            if source_writable != temp_writable:
                return False, (f"write-mode mismatch: source={oct(source_mode)}, temp={oct(temp_mode)}")
        else:
            if source_mode != temp_mode:
                return (
                    False,
                    (
                        "mode mismatch: "
                        f"source={oct(source_mode)}, "
                        f"temp={oct(temp_mode)}"
                    ),
                )

        # ----------------------------------------------------
        # Modification time
        # ----------------------------------------------------

        if temp_stat.st_mtime_ns != source_stat.st_mtime_ns:
            return (
                False,
                (
                    "mtime mismatch: "
                    f"source={source_stat.st_mtime_ns}, "
                    f"temp={temp_stat.st_mtime_ns}"
                ),
            )

        return True, None

    except OSError as exc:
        return (
            False,
            f"temporary validation failed: {exc}",
        )


# ============================================================
# Permissions
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
        pass


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
    except OSError:
        pass


# ============================================================
# Failure recording
# ============================================================


def failure_record(
    code: str,
    message: str,
) -> dict[str, Any]:

    return {
        "code": code,
        "message": message,
        "time": time.time(),
    }


# ============================================================
# Single-file refresh
# ============================================================


def refresh_one_file(
    root: Path,
    relative_path: str,
    expected_size: int,
    expected_mtime_ns: int,
) -> dict[str, Any]:
    """
    Refresh exactly one file.

    This function does NOT update SQLite.

    The caller is responsible for recording the returned
    result in the database.

    Return format:

        {
            "status": COMPLETED,
            "sha256": "...",
            "error_code": None,
            "error_message": None,
            "attempts": 1,
        }

    or:

        {
            "status": FAILED,
            "sha256": None,
            "error_code": "...",
            "error_message": "...",
            "attempts": 3,
        }

    Safety rule:

        The original source is only replaced after the
        temporary file has passed all validation.
    """

    root = root.resolve()

    source = (
        root / Path(relative_path)
    )

    temp = source.with_name(
        source.name + TEMP_SUFFIX
    )

    attempts = 0

    original_mode: int | None = None

    # --------------------------------------------------------
    # Basic source check.
    # --------------------------------------------------------

    if not is_regular_file(source):
        return {
            "status": FAILED,
            "sha256": None,
            "error_code": MISSING_SOURCE,
            "error_message": (
                "source is missing or is not "
                "a regular file"
            ),
            "attempts": 0,
        }

    # --------------------------------------------------------
    # Source validation.
    #
    # We hash the source exactly once for this job.
    # --------------------------------------------------------

    valid, source_result = validate_source(
        source,
        expected_size,
        expected_mtime_ns,
    )

    if not valid:
        return {
            "status": FAILED,
            "sha256": None,
            "error_code": SOURCE_CHANGED,
            "error_message": source_result,
            "attempts": 0,
        }

    expected_hash = source_result

    # --------------------------------------------------------
    # Disk-space check.
    #
    # We need enough space for the temporary copy.
    # --------------------------------------------------------

    try:
        free = shutil.disk_usage(
            source.parent
        ).free

        required = (expected_size + SAFETY_MARGIN_BYTES)

        if free < required:
            return {
                "status": FAILED,
                "sha256": expected_hash,
                "error_code": NO_SPACE,
                "error_message": (
                    "insufficient free space: "
                    f"required={required}, "
                    f"free={free}"
                ),
                "attempts": 0,
            }

    except OSError as exc:
        return {
            "status": FAILED,
            "sha256": expected_hash,
            "error_code": ERROR,
            "error_message": (
                f"unable to check disk space: {exc}"
            ),
            "attempts": 0,
        }

    # --------------------------------------------------------
    # Refresh attempts.
    # --------------------------------------------------------

    try:

        for attempt in range(
            1,
            MAX_RETRIES + 1,
        ):

            attempts = attempt

            remove_temp(temp)

            try:

                # ------------------------------------------------
                # Make source writable if required.
                # ------------------------------------------------

                original_mode = ensure_writable(
                    source
                )

                # ------------------------------------------------
                # Copy source -> temporary file.
                # ------------------------------------------------

                copy_writethrough(
                    source,
                    temp,
                )

                # ------------------------------------------------
                # Verify temporary file.
                # ------------------------------------------------

                valid, error = validate_temp(
                    source,
                    temp,
                    expected_size,
                    expected_hash,
                )

                if not valid:

                    if attempt < MAX_RETRIES:
                        time.sleep(
                            RETRY_DELAY
                        )
                        continue

                    return {
                        "status": FAILED,
                        "sha256": expected_hash,
                        "error_code": TEMP_VERIFY,
                        "error_message": (
                            error
                            or "temporary verification failed"
                        ),
                        "attempts": attempts,
                    }

                # ------------------------------------------------
                # Atomic replacement.
                # ------------------------------------------------

                try:

                    os.replace(
                        temp,
                        source,
                    )

                except PermissionError as exc:

                    if attempt < MAX_RETRIES:
                        time.sleep(
                            RETRY_DELAY
                        )
                        continue

                    return {
                        "status": FAILED,
                        "sha256": expected_hash,
                        "error_code": REPLACE,
                        "error_message": str(exc),
                        "attempts": attempts,
                    }

                except OSError as exc:

                    if attempt < MAX_RETRIES:
                        time.sleep(
                            RETRY_DELAY
                        )
                        continue

                    return {
                        "status": FAILED,
                        "sha256": expected_hash,
                        "error_code": REPLACE,
                        "error_message": str(exc),
                        "attempts": attempts,
                    }

                # ------------------------------------------------
                # Success.
                # ------------------------------------------------

                return {
                    "status": COMPLETED,
                    "sha256": expected_hash,
                    "error_code": None,
                    "error_message": None,
                    "attempts": attempts,
                }

            except PermissionError as exc:

                if attempt < MAX_RETRIES:
                    time.sleep(
                        RETRY_DELAY
                    )
                    continue

                return {
                    "status": FAILED,
                    "sha256": expected_hash,
                    "error_code": PERMISSION,
                    "error_message": str(exc),
                    "attempts": attempts,
                }

            except OSError as exc:

                if attempt < MAX_RETRIES:
                    time.sleep(
                        RETRY_DELAY
                    )
                    continue

                return {
                    "status": FAILED,
                    "sha256": expected_hash,
                    "error_code": ERROR,
                    "error_message": str(exc),
                    "attempts": attempts,
                }

            except Exception as exc:

                return {
                    "status": FAILED,
                    "sha256": expected_hash,
                    "error_code": ERROR,
                    "error_message": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "attempts": attempts,
                }

    finally:

        # --------------------------------------------------------
        # NEVER leave a temporary file behind.
        # --------------------------------------------------------

        remove_temp(temp)

        # --------------------------------------------------------
        # Restore original permissions.
        # --------------------------------------------------------

        if (
            original_mode is not None
            and source.exists()
        ):
            restore_mode(
                source,
                original_mode,
            )

    return {
        "status": FAILED,
        "sha256": expected_hash,
        "error_code": ERROR,
        "error_message": "unexpected refresh termination",
        "attempts": attempts,
    }