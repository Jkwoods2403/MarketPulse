"""code_version() runs `git` without credentials: a provider key in the ingestion process's
environment must never reach the `git rev-parse` child process.

Every credential value here is SYNTHETIC; the test session has already removed real ones
(tests/isolation.py).
"""

import os
import subprocess
import sys
import typing
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import SecretStr

import marketpulse.ingestion.runs as runs
from marketpulse import __version__
from marketpulse.core.config import REPO_ROOT, Settings

SYNTHETIC = {
    "EIA_API_KEY": "synthetic-eia-key-not-real-000000000000",
    "FRED_API_KEY": "synthetic-fred-key-not-real-0000000000",
    "POSTGRES_PASSWORD": "synthetic-db-password-not-real",
    "PGPASSWORD": "synthetic-pg-password-not-real",
}
HARMLESS = ("MARKETPULSE_TEST_HARMLESS", "kept-for-git")  # an ordinary variable: must be kept


@pytest.fixture(autouse=True)
def fresh_code_version() -> Iterator[None]:
    """code_version() is cached per process; every test here computes it afresh."""
    runs.code_version.cache_clear()
    yield
    runs.code_version.cache_clear()


@pytest.fixture
def credentials(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    for name, value in SYNTHETIC.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(*HARMLESS)
    return SYNTHETIC


def record_subprocess_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record every subprocess.run call (runs.py uses this same module), still running it."""
    calls: list[dict[str, Any]] = []
    real_run = subprocess.run

    def recording_run(args: Any, **kwargs: Any) -> Any:
        calls.append({"args": args, **kwargs})
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    return calls


def expected_sha() -> str | None:
    """The commit as git itself reports it, independently of code_version()."""
    try:
        return subprocess.run(  # fixed argv; the same command code_version() runs
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


# --- what git receives ---------------------------------------------------------------------------


def test_git_receives_no_credential(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_subprocess_calls(monkeypatch)
    runs.code_version()
    assert [call["args"] for call in calls] == [["git", "rev-parse", "--short", "HEAD"]]
    env = calls[0]["env"]
    assert env is not None, "git must get an explicit environment, not the inherited one"
    assert not {name.upper() for name in env} & set(SYNTHETIC)
    assert not set(SYNTHETIC.values()) & set(env.values())


def test_git_receives_every_other_variable_unchanged(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = record_subprocess_calls(monkeypatch)
    runs.code_version()
    env = calls[0]["env"]
    expected = {k: v for k, v in os.environ.items() if k.upper() not in SYNTHETIC}
    assert env == expected
    assert env[HARMLESS[0]] == HARMLESS[1]
    assert "PATH" in {name.upper() for name in env}  # what git needs to run at all
    if sys.platform == "win32":
        assert "SYSTEMROOT" in {name.upper() for name in env}


def test_the_real_git_child_process_environment_has_no_credential(
    credentials: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: start a real child process with exactly the environment code_version()
    gives git, and let the child itself report which credential names it can see."""
    seen: list[str] = []
    real_run = subprocess.run

    def probing_run(args: Any, **kwargs: Any) -> Any:
        probe = real_run(  # fixed argv: this interpreter, reporting env names only
            [
                sys.executable,
                "-c",
                "import os, sys; names = set(sys.argv[1:]); "
                "print(','.join(sorted(n for n in os.environ if n.upper() in names)))",
                *SYNTHETIC,
            ],
            env=kwargs["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        seen.append(probe.stdout.strip())
        return real_run(args, **kwargs)

    monkeypatch.setattr(subprocess, "run", probing_run)
    runs.code_version()
    assert seen == [""]  # the child saw none of the four credential names


def test_the_parent_environment_is_not_modified(credentials: dict[str, str]) -> None:
    before = dict(os.environ)
    runs.code_version()
    assert dict(os.environ) == before
    assert all(os.environ[name] == value for name, value in credentials.items())


# --- the filtering rule --------------------------------------------------------------------------


def test_credentials_are_excluded_case_insensitively() -> None:
    source = {
        "eia_api_key": "synthetic",  # Linux: pydantic-settings reads names case-insensitively
        "Fred_Api_Key": "synthetic",
        "POSTGRES_PASSWORD": "synthetic",
        "PGPASSWORD": "synthetic",
        "PATH": "/usr/bin",
        "HOME": "/home/someone",
        "POSTGRES_HOST": "127.0.0.1",  # connection settings that are not secrets are kept
    }
    copy = dict(source)
    assert runs.subprocess_environment(source) == {
        "PATH": "/usr/bin",
        "HOME": "/home/someone",
        "POSTGRES_HOST": "127.0.0.1",
    }
    assert source == copy  # the source mapping is not modified


def test_every_secret_setting_is_excluded() -> None:
    """A new SecretStr setting must also be added to SUBPROCESS_EXCLUDED_ENV."""
    secret_settings = {
        name.upper()
        for name, field in Settings.model_fields.items()
        if field.annotation is SecretStr or SecretStr in typing.get_args(field.annotation)
    }
    assert secret_settings == {"EIA_API_KEY", "FRED_API_KEY", "POSTGRES_PASSWORD"}
    assert secret_settings <= runs.SUBPROCESS_EXCLUDED_ENV
    assert "PGPASSWORD" in runs.SUBPROCESS_EXCLUDED_ENV  # libpq's own password variable


# --- commit identification and error handling are unchanged ------------------------------------


def test_commit_identification_still_works(credentials: dict[str, str]) -> None:
    sha = expected_sha()
    if sha:
        assert runs.code_version() == f"{__version__}+{sha}"
    else:  # not a git checkout, or git unavailable: the package version alone, as before
        assert runs.code_version() == __version__


def test_the_result_is_cached_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = record_subprocess_calls(monkeypatch)
    first = runs.code_version()
    assert runs.code_version() == first
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        OSError("git not installed"),
        subprocess.CalledProcessError(128, ["git"], stderr="not a git repository"),
        subprocess.TimeoutExpired(["git"], 2),
    ],
    ids=["no-git", "not-a-repository", "timeout"],
)
def test_git_failures_fall_back_to_the_package_version(
    failure: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_run(args: Any, **kwargs: Any) -> Any:
        assert kwargs["env"] is not None
        raise failure

    monkeypatch.setattr(subprocess, "run", failing_run)
    assert runs.code_version() == __version__


def test_an_empty_answer_falls_back_to_the_package_version(monkeypatch: pytest.MonkeyPatch) -> None:
    def empty_run(args: Any, **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(args, 0, stdout="\n", stderr="")

    monkeypatch.setattr(subprocess, "run", empty_run)
    assert runs.code_version() == __version__
