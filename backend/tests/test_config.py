import pytest

from marketpulse.core.config import Environment, Settings

# Every environment variable Settings reads. The test session deliberately exports the PostgreSQL
# connection settings (POSTGRES_*) for the database tests, so a test about *defaults* must remove
# them itself, or it would see the session's configuration instead (e.g. a staging port).
SETTINGS_ENV = tuple(name.upper() for name in Settings.model_fields)


def clear_settings_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in SETTINGS_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_settings_env(monkeypatch)
    settings = Settings(_env_file=None)

    assert settings.marketpulse_env is Environment.DEVELOPMENT
    assert (
        settings.postgres_host,
        settings.postgres_port,
        settings.postgres_user,
        settings.postgres_db,
    ) == ("localhost", 5432, "marketpulse", "marketpulse")
    assert settings.postgres_password.get_secret_value() == ""


@pytest.mark.parametrize("inherited_port", ["5433", "6543"])
def test_defaults_ignore_an_inherited_non_default_connection(
    monkeypatch: pytest.MonkeyPatch, inherited_port: str
) -> None:
    """Regression: a session configured for another server (here a non-default port, as in
    staging) must not leak into the defaults once the environment is cleared."""
    monkeypatch.setenv("POSTGRES_PORT", inherited_port)
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_DB", "marketpulse_staging_test")
    assert Settings(_env_file=None).postgres_port == int(inherited_port)
    clear_settings_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert (settings.postgres_host, settings.postgres_port, settings.postgres_db) == (
        "localhost",
        5432,
        "marketpulse",
    )


def test_settings_env_covers_every_connection_field() -> None:
    assert {
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_DB",
    } <= set(SETTINGS_ENV)


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETPULSE_ENV", "production")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    settings = Settings(_env_file=None)

    assert settings.marketpulse_env is Environment.PRODUCTION
    assert settings.postgres_port == 6543


def test_password_is_never_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret-value")
    settings = Settings(_env_file=None)

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.model_dump())
    assert "super-secret-value" in settings.database_url  # only via the explicit accessor


def test_fred_api_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRED_API_KEY", "synthetic-fred-key-0123456789")
    settings = Settings(_env_file=None)

    assert settings.fred_api_key is not None
    assert settings.fred_api_key.get_secret_value() == "synthetic-fred-key-0123456789"
    assert "synthetic-fred-key-0123456789" not in repr(settings)
    assert "synthetic-fred-key-0123456789" not in str(settings.model_dump())


@pytest.mark.parametrize("value", [None, ""])
def test_fred_api_key_unset_or_empty_is_none(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("FRED_API_KEY", raising=False)
    else:
        monkeypatch.setenv("FRED_API_KEY", value)  # `FRED_API_KEY=` in .env means "not set"
    assert Settings(_env_file=None).fred_api_key is None


def test_eia_api_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EIA_API_KEY", "synthetic-eia-key-0123456789")
    settings = Settings(_env_file=None)
    assert settings.eia_api_key is not None
    assert settings.eia_api_key.get_secret_value() == "synthetic-eia-key-0123456789"
    assert "synthetic-eia-key-0123456789" not in repr(settings)
    assert "synthetic-eia-key-0123456789" not in str(settings.model_dump())


@pytest.mark.parametrize("value", [None, ""])
def test_eia_api_key_unset_or_empty_is_none(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("EIA_API_KEY", raising=False)
    else:
        monkeypatch.setenv("EIA_API_KEY", value)
    assert Settings(_env_file=None).eia_api_key is None


def test_eia_api_key_whitespace_only_is_kept_by_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only `KEY=` means "not set"; a blank value survives as a SecretStr. The EIA job and client
    reject it before any request (tests/db/test_eia_ingestion.py, tests/test_eia_client.py)."""
    monkeypatch.setenv("EIA_API_KEY", "   ")
    key = Settings(_env_file=None).eia_api_key
    assert key is not None and key.get_secret_value() == "   "


def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETPULSE_ENV", "staging-typo")
    with pytest.raises(ValueError, match="marketpulse_env"):
        Settings(_env_file=None)
