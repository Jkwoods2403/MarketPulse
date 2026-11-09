"""Logging setup: readable text in development, one JSON object per line in staging and production.

Application log lines identify the environment they came from. Uvicorn's own server and access
lines do not, because they do not pass through this handler.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from marketpulse.core.config import Environment, Settings
from marketpulse.core.redaction import redact_text

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}

# Third-party loggers that write request URLs (which may contain API keys) into log messages.
HTTP_LIBRARY_LOGGERS = ("httpx", "httpcore")


class RedactingFilter(logging.Filter):
    """Rewrite each record's message with credential query parameters redacted.

    Installed on HTTP library loggers and on the root handler, so a URL containing an API key
    can never reach a log destination, even at DEBUG level.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def install_http_log_redaction() -> None:
    """Attach a RedactingFilter to the HTTP library loggers. Idempotent."""
    for name in HTTP_LIBRARY_LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactingFilter) for f in logger.filters):
            logger.addFilter(RedactingFilter())


class JsonFormatter(logging.Formatter):
    """Format records as single-line JSON with a UTC timestamp and any `extra=` fields."""

    def __init__(self, environment: str) -> None:
        super().__init__()
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "environment": self.environment,
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
    environment = settings.marketpulse_env.value
    if settings.marketpulse_env in (Environment.STAGING, Environment.PRODUCTION):
        handler.setFormatter(JsonFormatter(environment))
    else:
        handler.setFormatter(
            logging.Formatter(f"%(asctime)s %(levelname)-7s [{environment}] %(name)s: %(message)s")
        )
    handler.addFilter(RedactingFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.marketpulse_log_level.value)

    # httpx logs every request URL at INFO; keep it quiet and redacted regardless.
    install_http_log_redaction()
    for name in HTTP_LIBRARY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
