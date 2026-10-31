"""Engine and session factory."""

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from marketpulse.core.config import Settings


def make_engine(settings: Settings, database: str | None = None) -> Engine:
    """Create an engine. All sessions use UTC, regardless of server or client settings."""
    return create_engine(
        settings.database_url_for(database),
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
