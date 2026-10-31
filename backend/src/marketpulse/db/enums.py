"""Controlled vocabularies stored as text with CHECK constraints.

Text + CHECK (rather than PostgreSQL ENUM types) keeps migrations simple: adding a value is a
constraint change, not an ALTER TYPE.
"""

from enum import StrEnum


class License(StrEnum):
    """Redistribution rights of a source or series (ADR-0008)."""

    PUBLIC_DOMAIN = "public_domain"
    ATTRIBUTION_REQUIRED = "attribution_required"
    PERSONAL_USE_ONLY = "personal_use_only"
    NO_REDISTRIBUTION = "no_redistribution"


class Frequency(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


class ObservationStatus(StrEnum):
    OK = "ok"
    PRELIMINARY = "preliminary"
    ESTIMATED = "estimated"
    # The source itself published "no value" for this period (e.g. FRED's "." on holidays).
    # This is the only status allowed with a NULL value.
    MISSING_AT_SOURCE = "missing_at_source"
    SUSPECT = "suspect"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def check_in(column: str, enum: type[StrEnum]) -> str:
    """SQL expression restricting `column` to the enum's values."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"
