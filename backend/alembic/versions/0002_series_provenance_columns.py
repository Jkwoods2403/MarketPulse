"""Add provenance columns to ref.series: original_source, release_name, reference_url, notes.

`original_source` records who produced the numbers (e.g. "U.S. Bureau of Labor Statistics" for
CPI served by FRED, or "Refinitiv, an LSEG business" for spot prices republished by EIA).
`reference_url` links each series to its official page.

ref.series is empty before Milestone 1.3 (the catalog sync is its first writer), so the
NOT NULL columns are added without defaults.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("series", sa.Column("original_source", sa.Text(), nullable=False), schema="ref")
    op.add_column("series", sa.Column("release_name", sa.Text()), schema="ref")
    op.add_column("series", sa.Column("reference_url", sa.Text(), nullable=False), schema="ref")
    op.add_column("series", sa.Column("notes", sa.Text()), schema="ref")


def downgrade() -> None:
    for column in ("notes", "reference_url", "release_name", "original_source"):
        op.drop_column("series", column, schema="ref")
