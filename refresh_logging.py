from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "refresh"
DEFAULT_LOG_DIR = Path.home() / ".refresh" / "logs"
DEFAULT_LOG_FILE = "refresh.log"


def configure_logging(
    log_path: Path | None = None,
    *,
    verbose: bool = False,
) -> logging.Logger:
    """
    Configure application logging.

    Calling this function again replaces the existing refresh
    handlers, which is important when:
        - tests use different temporary log files
        - the CLI explicitly supplies --log
        - the application is embedded/restarted in the same process
    """

    path = (
        log_path
        or (DEFAULT_LOG_DIR / DEFAULT_LOG_FILE)
    ).expanduser().resolve()

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    logger = logging.getLogger(LOGGER_NAME)

    logger.setLevel(
        logging.DEBUG if verbose else logging.INFO
    )

    logger.propagate = False

    # --------------------------------------------------------
    # Remove existing handlers.
    #
    # Do not try to selectively reuse them. The caller has
    # explicitly supplied the desired logging destination.
    # --------------------------------------------------------

    for handler in logger.handlers[:]:

        try:
            handler.flush()
        finally:
            handler.close()

        logger.removeHandler(handler)

    # --------------------------------------------------------
    # Create the requested file handler.
    # --------------------------------------------------------

    handler = RotatingFileHandler(
        path,
        maxBytes=10 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )

    handler.setLevel(
        logging.DEBUG if verbose else logging.INFO
    )

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.addHandler(handler)

    # Make the file exist immediately. This also makes tests
    # deterministic even if no event has been emitted yet.
    handler.flush()

    return logger

def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def event(
    name: str,
    **fields: object,
) -> None:
    """Write one structured, human-readable application event."""

    logger = get_logger()

    parts = [name]

    for key, value in fields.items():

        if value is None:
            continue

        value_text = str(value).replace(
            "\n",
            "\\n",
        )

        parts.append(
            f"{key}={value_text}"
        )

    logger.info(
        " | ".join(parts)
    )

    # Keep the log immediately observable.
    for handler in logger.handlers:
        handler.flush()


