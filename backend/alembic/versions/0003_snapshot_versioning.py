"""Snapshot versioning support (Milestone 1.5 Phase D).

ref.series.vintage_basis
    What a series' version timestamps mean: "source_vintage" (provider vintages, FRED/ALFRED)
    or "first_seen" (first retrieved by Market Pulse, EIA; not a publication time). Existing
    series are labeled from their source; the migration fails if a series belongs to a source
    it cannot label, instead of guessing.

obs.observations.source_period, raw_payload_id, last_confirmed_run_id
    Provenance for first_seen versions: the period exactly as the provider reported it, the raw
    response page where the version first appeared, and the run that last confirmed it. All
    nullable: existing (FRED) rows keep NULL, meaning "not recorded". Adding nullable columns
    without defaults does not rewrite existing rows.

ops.data_quality_issues.last_detected_at, last_detected_run_id, occurrences, resolved_at,
resolved_by_run_id
    Lets a repeated finding (EIA "vanished_period") stay one open issue whose detection time and
    count are updated, instead of one new row per run. Existing rows: last_detected_at =
    created_at, occurrences = 1, unresolved.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    # --- ref.series.vintage_basis -------------------------------------------------------------
    op.add_column("series", sa.Column("vintage_basis", sa.Text()), schema="ref")
    op.execute(
        """
        UPDATE ref.series AS s
        SET vintage_basis = CASE src.code
            WHEN 'fred' THEN 'source_vintage'
            WHEN 'eia' THEN 'first_seen'
        END
        FROM ref.sources AS src
        WHERE src.id = s.source_id
        """
    )
    # Fails (NOT NULL violation) if any series could not be labeled.
    op.alter_column("series", "vintage_basis", nullable=False, schema="ref")
    op.create_check_constraint(
        op.f("ck_series_vintage_basis_valid"),
        "series",
        "vintage_basis IN ('source_vintage', 'first_seen')",
        schema="ref",
    )

    # --- obs.observations provenance ----------------------------------------------------------
    op.add_column("observations", sa.Column("source_period", sa.Text()), schema="obs")
    op.add_column("observations", sa.Column("raw_payload_id", sa.BigInteger()), schema="obs")
    op.add_column("observations", sa.Column("last_confirmed_run_id", sa.BigInteger()), schema="obs")
    op.create_foreign_key(
        "fk_observations_raw_payload_id_raw_payloads",
        "observations",
        "raw_payloads",
        ["raw_payload_id"],
        ["id"],
        source_schema="obs",
        referent_schema="ops",
    )
    op.create_foreign_key(
        "fk_observations_last_confirmed_run_id_ingestion_runs",
        "observations",
        "ingestion_runs",
        ["last_confirmed_run_id"],
        ["id"],
        source_schema="obs",
        referent_schema="ops",
    )

    # --- ops.data_quality_issues tracking -----------------------------------------------------
    op.add_column("data_quality_issues", sa.Column("last_detected_at", TS), schema="ops")
    op.execute("UPDATE ops.data_quality_issues SET last_detected_at = created_at")
    op.alter_column(
        "data_quality_issues",
        "last_detected_at",
        nullable=False,
        server_default=sa.text("now()"),
        schema="ops",
    )
    op.add_column(
        "data_quality_issues", sa.Column("last_detected_run_id", sa.BigInteger()), schema="ops"
    )
    op.add_column(
        "data_quality_issues",
        sa.Column("occurrences", sa.Integer(), server_default=sa.text("1"), nullable=False),
        schema="ops",
    )
    op.add_column("data_quality_issues", sa.Column("resolved_at", TS), schema="ops")
    op.add_column(
        "data_quality_issues", sa.Column("resolved_by_run_id", sa.BigInteger()), schema="ops"
    )
    for column in ("last_detected_run_id", "resolved_by_run_id"):
        op.create_foreign_key(
            f"fk_data_quality_issues_{column}_ingestion_runs",
            "data_quality_issues",
            "ingestion_runs",
            [column],
            ["id"],
            source_schema="ops",
            referent_schema="ops",
            ondelete="CASCADE",
        )
    op.create_check_constraint(
        op.f("ck_data_quality_issues_occurrences_positive"),
        "data_quality_issues",
        "occurrences >= 1",
        schema="ops",
    )
    op.create_check_constraint(
        op.f("ck_data_quality_issues_resolution_complete"),
        "data_quality_issues",
        "(resolved_at IS NULL) = (resolved_by_run_id IS NULL)",
        schema="ops",
    )
    op.create_index(
        "uq_data_quality_issues_open_vanished_period",
        "data_quality_issues",
        ["series_id", "period_start"],
        unique=True,
        schema="ops",
        postgresql_where=sa.text("check_name = 'vanished_period' AND resolved_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_data_quality_issues_open_vanished_period",
        table_name="data_quality_issues",
        schema="ops",
    )
    for name in (
        "ck_data_quality_issues_resolution_complete",
        "ck_data_quality_issues_occurrences_positive",
    ):
        op.drop_constraint(op.f(name), "data_quality_issues", type_="check", schema="ops")
    for column in (
        "resolved_by_run_id",
        "resolved_at",
        "occurrences",
        "last_detected_run_id",
        "last_detected_at",
    ):
        op.drop_column("data_quality_issues", column, schema="ops")
    for column in ("last_confirmed_run_id", "raw_payload_id", "source_period"):
        op.drop_column("observations", column, schema="obs")
    op.drop_constraint(op.f("ck_series_vintage_basis_valid"), "series", type_="check", schema="ref")
    op.drop_column("series", "vintage_basis", schema="ref")
