"""ORM models for the Milestone 1 schemas: ref, obs, ops (ADR-0007).

Lineage invariants enforced here and in the database:
- every observation belongs to a series, which belongs to a source (with its license);
- observations are append-only versions: a revision closes the old row (vintage_end) and
  inserts a new one, and at most one current row exists per (series, period);
- a NULL value is only allowed when the source itself reported no value.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Interval,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from marketpulse.db.base import Base
from marketpulse.db.enums import (
    Frequency,
    License,
    ObservationStatus,
    RunStatus,
    Severity,
    VintageBasis,
    check_in,
)

TIMESTAMPTZ = DateTime(timezone=True)


# --- ref -------------------------------------------------------------------------------------


class Source(Base):
    """A data provider (EIA, FRED, ...) and its terms."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(check_in("license", License), name="license_valid"),
        {"schema": "ref"},
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    homepage_url: Mapped[str] = mapped_column(Text)
    terms_url: Mapped[str | None] = mapped_column(Text)
    license: Mapped[str] = mapped_column(Text)
    attribution_text: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


class Series(Base):
    """A tracked indicator, e.g. EIA WTI spot price or FRED 10-year Treasury yield."""

    __tablename__ = "series"
    __table_args__ = (
        UniqueConstraint("source_id", "source_series_id"),
        CheckConstraint(check_in("frequency", Frequency), name="frequency_valid"),
        CheckConstraint(
            f"license_override IS NULL OR {check_in('license_override', License)}",
            name="license_override_valid",
        ),
        CheckConstraint("max_staleness > interval '0'", name="max_staleness_positive"),
        CheckConstraint(check_in("vintage_basis", VintageBasis), name="vintage_basis_valid"),
        {"schema": "ref"},
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("ref.sources.id"))
    source_series_id: Mapped[str] = mapped_column(Text)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str] = mapped_column(Text)
    frequency: Mapped[str] = mapped_column(Text)
    period_convention: Mapped[str] = mapped_column(Text)
    units: Mapped[str] = mapped_column(Text)
    seasonal_adjustment: Mapped[str | None] = mapped_column(Text)
    # Who produced the numbers (may differ from the provider, e.g. BLS data served by FRED).
    original_source: Mapped[str] = mapped_column(Text)
    release_name: Mapped[str | None] = mapped_column(Text)
    reference_url: Mapped[str] = mapped_column(Text)  # official series page
    notes: Mapped[str | None] = mapped_column(Text)
    license_override: Mapped[str | None] = mapped_column(Text)
    # What version timestamps mean for this series (from its source; immutable once synced):
    # "source_vintage" (provider vintages, FRED) or "first_seen" (first retrieved by Market
    # Pulse, EIA; not a publication time).
    vintage_basis: Mapped[str] = mapped_column(Text)
    max_staleness: Mapped[timedelta] = mapped_column(Interval)
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())


# --- ops -------------------------------------------------------------------------------------


class IngestionRun(Base):
    """One execution of an ingestion job, successful or not."""

    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint(check_in("status", RunStatus), name="status_valid"),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at", name="finished_after_started"
        ),
        Index("ix_ingestion_runs_job_started_at", "job", "started_at"),
        {"schema": "ops"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    job: Mapped[str] = mapped_column(Text)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("ref.sources.id"))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    started_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    status: Mapped[str] = mapped_column(Text, server_default=RunStatus.RUNNING.value)
    rows_fetched: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_inserted: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_revised: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    rows_unchanged: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    error_message: Mapped[str | None] = mapped_column(Text)
    code_version: Mapped[str | None] = mapped_column(Text)


class RawPayload(Base):
    """Pointer to a saved, compressed provider response. The URL never contains API keys."""

    __tablename__ = "raw_payloads"
    __table_args__ = ({"schema": "ops"},)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ops.ingestion_runs.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("ref.sources.id"))
    request_url: Mapped[str] = mapped_column(Text)
    http_status: Mapped[int] = mapped_column(SmallInteger)
    retrieved_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    file_path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)


class DataQualityIssue(Base):
    """A validation finding recorded during ingestion.

    Most issues are one row per detection. A *tracked* issue (currently EIA "vanished_period")
    stays one open row per series and period: repeated detections update last_detected_at,
    last_detected_run_id, and occurrences, and the row is resolved (resolved_at,
    resolved_by_run_id) when the period is reported again. ingestion_run_id and created_at
    always keep the first detection.
    """

    __tablename__ = "data_quality_issues"
    __table_args__ = (
        CheckConstraint(check_in("severity", Severity), name="severity_valid"),
        CheckConstraint("occurrences >= 1", name="occurrences_positive"),
        CheckConstraint(
            "(resolved_at IS NULL) = (resolved_by_run_id IS NULL)", name="resolution_complete"
        ),
        # At most one open "vanished_period" issue per series and period.
        Index(
            "uq_data_quality_issues_open_vanished_period",
            "series_id",
            "period_start",
            unique=True,
            postgresql_where=text("check_name = 'vanished_period' AND resolved_at IS NULL"),
        ),
        {"schema": "ops"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ingestion_run_id: Mapped[int] = mapped_column(
        ForeignKey("ops.ingestion_runs.id", ondelete="CASCADE"), index=True
    )
    series_id: Mapped[int | None] = mapped_column(ForeignKey("ref.series.id"))
    period_start: Mapped[date | None] = mapped_column(Date)
    check_name: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_detected_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ, server_default=func.now())
    last_detected_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ops.ingestion_runs.id", ondelete="CASCADE")
    )
    occurrences: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    resolved_by_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("ops.ingestion_runs.id", ondelete="CASCADE")
    )


# --- obs -------------------------------------------------------------------------------------


class Observation(Base):
    """One version of one series value for one period. Append-only.

    The meaning of [vintage_start, vintage_end) depends on the series' vintage_basis: provider
    vintages for "source_vintage" series, first-seen-by-Market-Pulse snapshots for "first_seen"
    series. Only vintage_end (once, when superseded) and the confirmation fields change.
    """

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("series_id", "period_start", "vintage_start"),
        CheckConstraint("period_end >= period_start", name="period_order"),
        CheckConstraint("vintage_end IS NULL OR vintage_end > vintage_start", name="vintage_order"),
        CheckConstraint(check_in("status", ObservationStatus), name="status_valid"),
        CheckConstraint(
            f"(value IS NULL) = (status = '{ObservationStatus.MISSING_AT_SOURCE.value}')",
            name="null_value_only_when_missing_at_source",
        ),
        # At most one current (unrevised) row per series and period.
        Index(
            "uq_observations_current",
            "series_id",
            "period_start",
            unique=True,
            postgresql_where=text("vintage_end IS NULL"),
        ),
        {"schema": "obs"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("ref.series.id"))
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[Decimal | None] = mapped_column(Numeric)
    status: Mapped[str] = mapped_column(Text)
    vintage_start: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    vintage_end: Mapped[datetime | None] = mapped_column(TIMESTAMPTZ)
    first_retrieved_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    last_confirmed_at: Mapped[datetime] = mapped_column(TIMESTAMPTZ)
    # The run that created this version (never changes).
    ingestion_run_id: Mapped[int] = mapped_column(ForeignKey("ops.ingestion_runs.id"))
    # Provenance recorded for first_seen (EIA) versions; NULL for FRED versions, where it was
    # not recorded. source_period is the period exactly as the provider reported it, and
    # raw_payload_id the raw response page where this version first appeared (never changes).
    source_period: Mapped[str | None] = mapped_column(Text)
    raw_payload_id: Mapped[int | None] = mapped_column(ForeignKey("ops.raw_payloads.id"))
    # The run that last confirmed this version unchanged (first_seen writes only).
    last_confirmed_run_id: Mapped[int | None] = mapped_column(ForeignKey("ops.ingestion_runs.id"))
