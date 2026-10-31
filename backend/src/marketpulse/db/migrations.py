"""Programmatic access to Alembic migrations (used by the CLI and tests)."""

from pathlib import Path

from alembic import command
from alembic.config import Config

# backend/src/marketpulse/db/migrations.py -> backend/
BACKEND_DIR = Path(__file__).resolve().parents[3]
ALEMBIC_INI = BACKEND_DIR / "alembic.ini"


def alembic_config(url: str | None = None, *, configure_logger: bool = True) -> Config:
    """Alembic config, optionally targeting a specific database URL (never written to disk)."""
    config = Config(str(ALEMBIC_INI))
    if url is not None:
        config.attributes["url"] = url
    config.attributes["configure_logger"] = configure_logger
    return config


def upgrade(url: str | None = None, revision: str = "head", *, sql: bool = False) -> None:
    command.upgrade(alembic_config(url), revision, sql=sql)


def downgrade(url: str | None = None, revision: str = "base") -> None:
    command.downgrade(alembic_config(url), revision)


def current(url: str | None = None) -> None:
    command.current(alembic_config(url), verbose=False)
