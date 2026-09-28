from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LOG_LEVEL = "INFO"
MAX_LOG_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_SECRET_PATTERNS = (
    re.compile(
        r"(?i)\b(MYSQL_PASSWORD|MONITOR_ADMIN_TOKEN|CLIENT_AUTH_TOKEN|"
        r"ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|TOKEN)(\s*[:=]\s*)([^\s,;]+)"
    ),
    re.compile(r"(?i)\b(Authorization\s*:\s*Bearer\s+)([^\s,;]+)"),
)


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self._redact(
                logging.Formatter().formatException(record.exc_info)
            )
            record.exc_info = None
        return True

    @staticmethod
    def _redact(message: str) -> str:
        for pattern in _SECRET_PATTERNS:
            if pattern.groups == 3:
                message = pattern.sub(r"\1\2[REDACTED]", message)
            else:
                message = pattern.sub(r"\1[REDACTED]", message)
        return message


def _configured_level() -> tuple[int, str | None]:
    requested = os.environ.get("LOG_LEVEL", DEFAULT_LOG_LEVEL).strip().upper()
    level = getattr(logging, requested, None)
    if not isinstance(level, int):
        return logging.INFO, requested
    return level, None


def _log_path(component: Literal["server", "client"], explicit_path: str | Path | None) -> Path:
    if explicit_path is not None:
        configured_path = Path(explicit_path)
    elif component == "server":
        configured_path = Path(os.environ.get("LOG_FILE", "logs/server.log"))
    else:
        configured_path = Path(os.environ.get("CLIENT_LOG_FILE", "logs/client.log"))
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return configured_path.resolve()


def configure_logging(
    component: Literal["server", "client"],
    log_file: str | Path | None = None,
) -> Path:
    level, invalid_level = _configured_level()
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    log_path = _log_path(component, log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = next(
        (
            handler
            for handler in root_logger.handlers
            if getattr(handler, "_monitor_console_handler", False)
        ),
        None,
    )
    if console_handler is None:
        console_handler = logging.StreamHandler()
        console_handler._monitor_console_handler = True
        console_handler.addFilter(SecretRedactionFilter())
        root_logger.addHandler(console_handler)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    file_handler = next(
        (
            handler
            for handler in root_logger.handlers
            if getattr(handler, "_monitor_log_path", None) == str(log_path)
        ),
        None,
    )
    if file_handler is None:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler._monitor_log_path = str(log_path)
        file_handler.addFilter(SecretRedactionFilter())
        root_logger.addHandler(file_handler)
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    if invalid_level is not None:
        logging.getLogger(__name__).warning(
            "Invalid LOG_LEVEL %r; using INFO.",
            invalid_level,
        )
    return log_path
