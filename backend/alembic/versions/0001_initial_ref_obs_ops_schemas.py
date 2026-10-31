"""Initial schemas: ref (sources, series), obs (observations), ops (runs, payloads, quality).

Values in CHECK constraints are written out literally (not imported from application code) so
this migration never changes when the application's enums change later.

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LICENSES = "'public_domain', 'attribution_required', 'personal_use_only', 'no_redistribution'"
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    for schema in ("ref", "obs", "ops"):
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")

    # --- ref.sources --------------------------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", sa.SmallInteger(), primary_key=True),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=False),
        sa.Column("terms_url", sa.Text()),
        sa.Column("license", sa.Text(), nullable=False),
        sa.Column("attribution_text", sa.Text()),
        sa.Column("notes", sa.Text()),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sources"),
        sa.UniqueConstraint("code", name="uq_sources_code"),
        sa.CheckConstraint(f"license IN ({LICENSES})", name=op.f("ck_sources_license_valid")),
        schema="ref",
    )

    # --- ref.series ---------------------------------------------------------------------------
    op.create_table(
        "series",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.SmallInteger(), nullable=False),
        sa.Column("source_series_id", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("frequency", sa.Text(), nullable=False),
        sa.Column("period_convention", sa.Text(), nullable=False),
        sa.Column("units", sa.Text(), nullable=False),
        sa.Column("seasonal_adjustment", sa.Text()),
        sa.Column("license_override", sa.Text()),
        sa.Column("max_staleness", sa.Interval(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_series"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["ref.sources.id"], name="fk_series_source_id_sources"
        ),
        sa.UniqueConstraint(
            "source_id", "source_series_id", name="uq_series_source_id_source_series_id"
        ),
        sa.UniqueConstraint("code", name="uq_series_code"),
        sa.CheckConstraint(
            "frequency IN ('daily', 'weekly', 'monthly', 'quarterly', 'annual')",
            name=op.f("ck_series_frequency_valid"),
        ),
        sa.CheckConstraint(
            f"license_override IS NULL OR license_override IN ({LICENSES})",
            name=op.f("ck_series_license_override_valid"),
        ),
        sa.CheckConstraint(
            "max_staleness > interval '0'", name=op.f("ck_series_max_staleness_positive")
        ),
        schema="ref",
    )

    # --- ops.ingestion_runs ------------------------------------------------------------------
    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("job", sa.Text(), nullable=False),
        sa.Column("source_id", sa.SmallInteger()),
        sa.Column(
            "params",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("started_at", TS, server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", TS),
        sa.Column("status", sa.Text(), server_default=sa.text("'running'"), nullable=False),
        sa.Column("rows_fetched", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_inserted", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_revised", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_unchanged", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("code_version", sa.Text()),
        sa.PrimaryKeyConstraint("id", name="pk_ingestion_runs"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["ref.sources.id"], name="fk_ingestion_runs_source_id_sources"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_ingestion_runs_status_valid"),
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name=op.f("ck_ingestion_runs_finished_after_started"),
        ),
        schema="ops",
    )
    op.create_index(
        "ix_ingestion_runs_job_started_at",
        "ingestion_runs",
        ["job", "started_at"],
        schema="ops",
    )

    # --- ops.raw_payloads --------------------------------------------------------------------
    op.create_table(
        "raw_payloads",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ingestion_run_id", sa.BigInteger(), nullable=False),
        sa.Column("source_id", sa.SmallInteger(), nullable=False),
        sa.Column("request_url", sa.Text(), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=False),
        sa.Column("retrieved_at", TS, nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_raw_payloads"),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ops.ingestion_runs.id"],
            name="fk_raw_payloads_ingestion_run_id_ingestion_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["ref.sources.id"], name="fk_raw_payloads_source_id_sources"
        ),
        schema="ops",
    )
    op.create_index(
        "ix_raw_payloads_ingestion_run_id", "raw_payloads", ["ingestion_run_id"], schema="ops"
    )

    # --- ops.data_quality_issues -------------------------------------------------------------
    op.create_table(
        "data_quality_issues",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("ingestion_run_id", sa.BigInteger(), nullable=False),
        sa.Column("series_id", sa.Integer()),
        sa.Column("period_start", sa.Date()),
        sa.Column("check_name", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("created_at", TS, server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_data_quality_issues"),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ops.ingestion_runs.id"],
            name="fk_data_quality_issues_ingestion_run_id_ingestion_runs",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["ref.series.id"], name="fk_data_quality_issues_series_id_series"
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'error')",
            name=op.f("ck_data_quality_issues_severity_valid"),
        ),
        schema="ops",
    )
    op.create_index(
        "ix_data_quality_issues_ingestion_run_id",
        "data_quality_issues",
        ["ingestion_run_id"],
        schema="ops",
    )

    # --- obs.observations --------------------------------------------------------------------
    op.create_table(
        "observations",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("vintage_start", TS, nullable=False),
        sa.Column("vintage_end", TS),
        sa.Column("first_retrieved_at", TS, nullable=False),
        sa.Column("last_confirmed_at", TS, nullable=False),
        sa.Column("ingestion_run_id", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_observations"),
        sa.ForeignKeyConstraint(
            ["series_id"], ["ref.series.id"], name="fk_observations_series_id_series"
        ),
        sa.ForeignKeyConstraint(
            ["ingestion_run_id"],
            ["ops.ingestion_runs.id"],
            name="fk_observations_ingestion_run_id_ingestion_runs",
        ),
        sa.UniqueConstraint(
            "series_id",
            "period_start",
            "vintage_start",
            name="uq_observations_series_id_period_start_vintage_start",
        ),
        sa.CheckConstraint("period_end >= period_start", name=op.f("ck_observations_period_order")),
        sa.CheckConstraint(
            "vintage_end IS NULL OR vintage_end > vintage_start",
            name=op.f("ck_observations_vintage_order"),
        ),
        sa.CheckConstraint(
            "status IN ('ok', 'preliminary', 'estimated', 'missing_at_source', 'suspect')",
            name=op.f("ck_observations_status_valid"),
        ),
        sa.CheckConstraint(
            "(value IS NULL) = (status = 'missing_at_source')",
            name=op.f("ck_observations_null_value_only_when_missing_at_source"),
        ),
        schema="obs",
    )
    op.create_index(
        "uq_observations_current",
        "observations",
        ["series_id", "period_start"],
        unique=True,
        schema="obs",
        postgresql_where=sa.text("vintage_end IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("observations", schema="obs")
    op.drop_table("data_quality_issues", schema="ops")
    op.drop_table("raw_payloads", schema="ops")
    op.drop_table("ingestion_runs", schema="ops")
    op.drop_table("series", schema="ref")
    op.drop_table("sources", schema="ref")
    for schema in ("ops", "obs", "ref"):
        op.execute(f"DROP SCHEMA IF EXISTS {schema}")
