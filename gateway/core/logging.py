"""Structured JSON logging strictly bound to sys.stderr."""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class StderrJsonFormatter(logging.Formatter):
    """Machine-parsable JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure_stderr_logging(level: str = "INFO", logger_name: str = "quilr") -> logging.Logger:
    """Configure unbuffered structured logging exclusively on stderr."""
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(StderrJsonFormatter())
    logger.addHandler(handler)
    return logger