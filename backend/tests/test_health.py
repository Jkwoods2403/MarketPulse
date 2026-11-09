from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from marketpulse import __version__
from marketpulse.api.main import create_app
from marketpulse.core.config import Environment, Settings


def make_client() -> TestClient:
    # _env_file=None: never read a developer's local .env during tests.
    settings = Settings(_env_file=None, marketpulse_env=Environment.TEST)  # type: ignore[call-arg]
    return TestClient(create_app(settings))


def test_health_reports_ok_version_and_environment() -> None:
    response = make_client().get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["environment"] == "test"
    assert body["data_mode"] == "database"  # never synthetic outside the dev app


def test_health_server_time_is_timezone_aware_utc() -> None:
    body = make_client().get("/health").json()

    server_time = datetime.fromisoformat(body["server_time_utc"])
    assert server_time.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - server_time) < timedelta(seconds=30)
