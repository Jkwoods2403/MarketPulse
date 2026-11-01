import io
import logging

from marketpulse.core.config import Environment, Settings
from marketpulse.core.logging import RedactingFilter, configure_logging

SECRET = "TEST-SECRET-9f8e7d6c5b4a"


def test_redacting_filter_rewrites_messages_with_args() -> None:
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: GET %s",
        (f"https://x.test/?api_key={SECRET}",),
        None,
    )
    RedactingFilter().filter(record)
    assert SECRET not in record.getMessage()
    assert "api_key=REDACTED" in record.getMessage()


def test_configured_root_handler_redacts_any_logger() -> None:
    settings = Settings(_env_file=None, marketpulse_env=Environment.PRODUCTION)  # type: ignore[call-arg]
    configure_logging(settings)
    handler = logging.getLogger().handlers[0]
    stream = io.StringIO()
    handler.setStream(stream)  # type: ignore[attr-defined]

    logging.getLogger("some.other.library").warning("calling https://x.test/?token=%s", SECRET)

    output = stream.getvalue()
    assert SECRET not in output
    assert "token=REDACTED" in output


def test_configure_logging_quiets_and_filters_http_libraries() -> None:
    configure_logging(Settings(_env_file=None))  # type: ignore[call-arg]
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert logger.level == logging.WARNING
        assert any(isinstance(f, RedactingFilter) for f in logger.filters)
