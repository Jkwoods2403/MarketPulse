"""Logging setup: readable text in development, one JSON object per line in production."""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from marketpulse.core.config import Environment, Settings

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON with a UTC timestamp and any `extra=` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Configure the root logger. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    if settings.marketpulse_env is Environment.PRODUCTION:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.marketpulse_log_level.value)
