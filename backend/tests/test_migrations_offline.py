"""Static checks that run without a database.

Renders the migrations as SQL (Alembic offline mode) and verifies that every table, constraint,
and index defined by the ORM models appears in the migration with exactly the same name.
The full structural comparison against a live database is in tests/db/test_migrations.py.
"""

import io
import re
from contextlib import redirect_stdout

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from marketpulse.db import migrations, models  # noqa: F401  (registers tables)
from marketpulse.db.base import SCHEMAS, Base

NAME = re.compile(r"(?:CONSTRAINT|INDEX) (\w+)")


def render_migration_sql() -> str:
    buffer = io.StringIO()
    config = migrations.alembic_config(
        "postgresql+psycopg://offline@localhost/offline", configure_logger=False
    )
    config.output_buffer = buffer
    with redirect_stdout(io.StringIO()):
        from alembic import command

        command.upgrade(config, "head", sql=True)
    return buffer.getvalue()


def model_ddl() -> str:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    parts: list[str] = []
    for table in Base.metadata.sorted_tables:
        parts.append(str(CreateTable(table).compile(dialect=dialect)))
        parts.extend(str(CreateIndex(index).compile(dialect=dialect)) for index in table.indexes)
    return "\n".join(parts)


def test_migration_creates_all_schemas() -> None:
    sql = render_migration_sql()
    for schema in SCHEMAS:
        assert f"CREATE SCHEMA IF NOT EXISTS {schema}" in sql


def test_migration_creates_every_model_table() -> None:
    sql = render_migration_sql()
    for table in Base.metadata.sorted_tables:
        assert f"CREATE TABLE {table.schema}.{table.name} (" in sql, table.fullname


def test_constraint_and_index_names_match_models_exactly() -> None:
    model_names = set(NAME.findall(model_ddl()))
    migration_names = set(NAME.findall(render_migration_sql())) - {"alembic_version_pkc"}

    assert model_names, "expected the models to define named constraints"
    assert model_names == migration_names


def test_no_constraint_name_exceeds_postgres_limit() -> None:
    for name in NAME.findall(render_migration_sql()):
        assert len(name) <= 63, name
