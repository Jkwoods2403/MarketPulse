import pytest

from marketpulse.core.config import Environment, Settings


def test_defaults_without_env_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MARKETPULSE_ENV", raising=False)
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.marketpulse_env is Environment.DEVELOPMENT
    assert settings.postgres_port == 5432


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETPULSE_ENV", "production")
    monkeypatch.setenv("POSTGRES_PORT", "6543")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.marketpulse_env is Environment.PRODUCTION
    assert settings.postgres_port == 6543


def test_password_is_never_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "super-secret-value")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert "super-secret-value" not in repr(settings)
    assert "super-secret-value" not in str(settings.model_dump())
    assert "super-secret-value" in settings.database_url  # only via the explicit accessor


def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MARKETPULSE_ENV", "staging-typo")
    with pytest.raises(ValueError, match="marketpulse_env"):
        Settings(_env_file=None)  # type: ignore[call-arg]
