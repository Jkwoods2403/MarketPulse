"""Application configuration loaded from environment variables.

Values come from the process environment first, then from the repository-root `.env` file
(gitignored). Secrets are typed as `SecretStr` so they are never printed in logs or reprs.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/src/marketpulse/core/config.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[4]


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
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
    )

    marketpulse_env: Environment = Environment.DEVELOPMENT
    marketpulse_log_level: LogLevel = LogLevel.INFO

    postgres_user: str = "marketpulse"
    postgres_password: SecretStr = SecretStr("")
    postgres_db: str = "marketpulse"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    @property
    def database_url(self) -> str:
        """SQLAlchemy URL for the psycopg driver. Contains the password; never log it."""
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql+psycopg://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached)."""
    return Settings()
