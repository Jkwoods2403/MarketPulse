"""Source access requirement and sub-daily series frequencies (ERCOT Step 3A, ADR-0012/0014).

ref.sources.access_requirement
    How a source's data may be fetched, separate from its license (what may be done with it):
    "open", "registration_required" (a free account or API key), or "subscription_required"
    (an account plus a subscription to a data product). NOT NULL with no default: every source
    must be classified explicitly. Existing sources are classified by code (FRED and EIA both
    require a free API key); the migration fails (NOT NULL violation) if a source exists that it
    cannot classify, instead of guessing.

ref.series.frequency
    Adds "hourly" and "fifteen_minute" for interval data (ERCOT). "five_minute" is deferred.
    Downgrade refuses while any series uses a new value, so no series is left violating the
    narrower constraint.

No ERCOT tables, series, or data are created here (those follow the read-only probe, Step 3B).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ACCESS_REQUIREMENTS = "'open', 'registration_required', 'subscription_required'"
FREQUENCIES_0001 = "'daily', 'weekly', 'monthly', 'quarterly', 'annual'"
FREQUENCIES_0004 = f"{FREQUENCIES_0001}, 'hourly', 'fifteen_minute'"


def upgrade() -> None:
    # --- ref.sources.access_requirement -------------------------------------------------------
    op.add_column("sources", sa.Column("access_requirement", sa.Text()), schema="ref")
    op.execute(
        """
        UPDATE ref.sources
        SET access_requirement = CASE code
            WHEN 'fred' THEN 'registration_required'
            WHEN 'eia' THEN 'registration_required'
        END
        """
    )
    # Fails (NOT NULL violation) if any source could not be classified.
    op.alter_column("sources", "access_requirement", nullable=False, schema="ref")
    op.create_check_constraint(
        op.f("ck_sources_access_requirement_valid"),
        "sources",
        f"access_requirement IN ({ACCESS_REQUIREMENTS})",
        schema="ref",
    )

    # --- ref.series.frequency -----------------------------------------------------------------
    op.drop_constraint(op.f("ck_series_frequency_valid"), "series", type_="check", schema="ref")
    op.create_check_constraint(
        op.f("ck_series_frequency_valid"),
        "series",
        f"frequency IN ({FREQUENCIES_0004})",
        schema="ref",
    )


def downgrade() -> None:
    in_use = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM ref.series WHERE frequency IN ('hourly', 'fifteen_minute')"
            )
        )
        .scalar_one()
    )
    if in_use:
        raise RuntimeError(
            f"cannot downgrade below 0004: {in_use} series use a frequency added in 0004 "
            "(hourly or fifteen_minute); retire or remove them first"
        )
    op.drop_constraint(op.f("ck_series_frequency_valid"), "series", type_="check", schema="ref")
    op.create_check_constraint(
        op.f("ck_series_frequency_valid"),
        "series",
        f"frequency IN ({FREQUENCIES_0001})",
        schema="ref",
    )

    op.drop_constraint(
        op.f("ck_sources_access_requirement_valid"), "sources", type_="check", schema="ref"
    )
    op.drop_column("sources", "access_requirement", schema="ref")
