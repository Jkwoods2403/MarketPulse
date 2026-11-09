"""Application configuration loaded from environment variables.

Values come from the process environment first, then from the repository-root `.env` file
(gitignored). Secrets are typed as `SecretStr` so they are never printed in logs or reprs.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/marketpulse/core/config.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[4]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # `KEY=` in .env means "not set", not "empty string"
    )

    marketpulse_env: Environment = Environment.DEVELOPMENT
    marketpulse_log_level: LogLevel = LogLevel.INFO

    # Local storage for raw provider payloads (gitignored). See data/README.md.
    marketpulse_data_dir: Path = REPO_ROOT / "data"

    # Descriptive User-Agent with contact info, required by some providers (SEC EDGAR, NWS).
    http_user_agent: str | None = None

    # Provider credentials. SecretStr keeps them out of reprs and logs; None means not set.
    fred_api_key: SecretStr | None = None
    eia_api_key: SecretStr | None = None

    postgres_user: str = "marketpulse"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "marketpulse"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the configured database. Contains the password; never log it."""
        return self.database_url_for(None)

    def database_url_for(self, database: str | None) -> str:
        """SQLAlchemy URL (psycopg driver) for `database`, defaulting to POSTGRES_DB."""
        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")
        name = database or self.postgres_db
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{name}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
