"""Source-level ingestion block (ADR-0014) on every shared database write path.

A source row with code `ercot` exists here only to prove that nothing can be written for it:
no ingestion run, raw payload row, observation, or data-quality issue. A permitted control source
shows the block is specific to the blocked source. All rows are synthetic.

FRED (retired as a provider, 2026-11-11) is blocked the same way: its synced catalog source is
refused on every write path, and the FRED job and `marketpulse ingest fred`, pointed at this
database with a key and a simulated FRED API, write nothing and send no request.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from test_fred_ingestion import SPECS as FRED_SPECS
from test_fred_ingestion import dgs10_rows
from test_fred_ingestion import make_client_factory as fred_client_factory

import marketpulse.ingestion.fred as fred_job
from fred_mock import TEST_KEY as FRED_TEST_KEY
from fred_sim import FakeFred, SimSeries, metadata_for
from marketpulse.cli import main
from marketpulse.core.config import Settings, get_settings
from marketpulse.core.source_access import SourceIngestionBlockedError
from marketpulse.core.versions import ObservationVersion
from marketpulse.db.models import (
    DataQualityIssue,
    IngestionRun,
    Observation,
    RawPayload,
    Series,
    Source,
)
from marketpulse.ingestion.catalog_sync import sync_catalog
from marketpulse.ingestion.fred import ingest_fred
from marketpulse.ingestion.observation_writer import write_versions
from marketpulse.ingestion.raw_store import StoredPayload, record_raw_payload
from marketpulse.ingestion.runs import IngestionRunTracker, record_series_versions
from marketpulse.ingestion.snapshot_writer import write_snapshot
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope
from marketpulse.sources.catalog import load_catalog
from marketpulse.sources.http import FetchResult

T1 = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
P = date(2026, 7, 1)


def make_source(session: Session, code: str) -> int:
    source = Source(
        code=code,
        name=f"{code} (synthetic)",
        homepage_url="https://example.invalid",
        license="no_redistribution",
        access_requirement="registration_required",
    )
    session.add(source)
    session.flush()
    return source.id


def make_series(session: Session, source_id: int, code: str, vintage_basis: str) -> int:
    series = Series(
        source_id=source_id,
        source_series_id=code.upper(),
        code=code,
        name="Synthetic series",
        category="electricity",
        frequency="daily",
        period_convention="calendar_day",
        units="Synthetic units",
        original_source="Synthetic",
        reference_url="https://example.invalid/series",
        max_staleness=timedelta(days=7),
        vintage_basis=vintage_basis,
    )
    session.add(series)
    session.flush()
    return series.id


class Ids:
    def __init__(self, session: Session) -> None:
        self.ercot = make_source(session, "ercot")
        self.ercot_vintage_series = make_series(
            session, self.ercot, "ercot_sv_test", "source_vintage"
        )
        self.ercot_snapshot_series = make_series(session, self.ercot, "ercot_fs_test", "first_seen")
        self.control = make_source(session, "control_test")
        self.control_series = make_series(
            session, self.control, "control_sv_test", "source_vintage"
        )


@pytest.fixture
def ids(factory: sessionmaker[Session]) -> Iterator[Ids]:
    with factory() as s, s.begin():
        created = Ids(s)
    yield created


def count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def counts(factory: sessionmaker[Session]) -> dict[str, int]:
    return {
        "runs": count(factory, IngestionRun),
        "raw_payloads": count(factory, RawPayload),
        "observations": count(factory, Observation),
        "issues": count(factory, DataQualityIssue),
    }


NOTHING = {"runs": 0, "raw_payloads": 0, "observations": 0, "issues": 0}


def version(value: str) -> ObservationVersion:
    return ObservationVersion(
        P, date(2026, 9, 30), T1 - timedelta(days=1), None, Decimal(value), "ok"
    )


def test_run_tracker_refuses_a_blocked_source_before_the_run_row(
    factory: sessionmaker[Session], ids: Ids
) -> None:
    entered = False
    tracker = IngestionRunTracker(factory, job="ercot.observations", source_id=ids.ercot)
    with pytest.raises(SourceIngestionBlockedError), tracker:
        entered = True
    assert not entered  # the run body never ran
    assert tracker.run_id is None
    assert counts(factory) == NOTHING


def test_record_series_versions_refuses_a_blocked_source(
    factory: sessionmaker[Session], ids: Ids
) -> None:
    with pytest.raises(SourceIngestionBlockedError):
        record_series_versions(
            factory,
            job="ercot.observations",
            source_id=ids.ercot,
            series_id=ids.ercot_vintage_series,
            versions=[version("1.5")],
            retrieved_at=T1,
        )
    assert counts(factory) == NOTHING


def test_raw_payload_row_is_refused_for_a_blocked_source(
    factory: sessionmaker[Session], ids: Ids
) -> None:
    with factory() as s, s.begin():
        run = IngestionRun(job="test.unrelated")  # a run with no source is allowed
        s.add(run)
        s.flush()
        run_id = run.id
    with pytest.raises(SourceIngestionBlockedError), factory() as s, s.begin():
        record_raw_payload(
            s,
            ingestion_run_id=run_id,
            source_id=ids.ercot,
            result=FetchResult(
                content=b"{}",  # synthetic
                status_code=200,
                content_type="application/json",
                url="https://api.example.com/synthetic",
                retrieved_at=T1,
                elapsed_seconds=0.1,
                attempts=1,
            ),
            stored=StoredPayload("raw/ercot/x/2026/11/synthetic.json.gz", "0" * 64, 2),
        )
    assert counts(factory) == {**NOTHING, "runs": 1}


def test_observation_writer_refuses_a_blocked_source(
    factory: sessionmaker[Session], ids: Ids
) -> None:
    with factory() as s, s.begin():
        run = IngestionRun(job="test.unrelated")
        s.add(run)
        s.flush()
        run_id = run.id
    with pytest.raises(SourceIngestionBlockedError), factory() as s, s.begin():
        write_versions(
            s,
            series_id=ids.ercot_vintage_series,
            ingestion_run_id=run_id,
            versions=[version("1.5")],
            retrieved_at=T1,
        )
    assert counts(factory) == {**NOTHING, "runs": 1}


def test_snapshot_writer_refuses_a_blocked_source(factory: sessionmaker[Session], ids: Ids) -> None:
    with factory() as s, s.begin():
        run = IngestionRun(job="test.unrelated", started_at=T1 - timedelta(hours=1))
        s.add(run)
        s.flush()
        run_id = run.id
    with pytest.raises(SourceIngestionBlockedError), factory() as s, s.begin():
        write_snapshot(
            s,
            series_id=ids.ercot_snapshot_series,
            ingestion_run_id=run_id,
            snapshot_at=T1,
            scope=SnapshotScope(),
            period_format="YYYY-MM-DD",
            incoming=[
                SnapshotObservation(
                    source_period=P.isoformat(),
                    period_start=P,
                    period_end=P,
                    value=Decimal("1.5"),
                    status="ok",
                    raw_payload_id=None,
                )
            ],
        )
    assert counts(factory) == {**NOTHING, "runs": 1}


def test_a_permitted_source_is_unaffected(factory: sessionmaker[Session], ids: Ids) -> None:
    run_id, result = record_series_versions(
        factory,
        job="control.observations",
        source_id=ids.control,
        series_id=ids.control_series,
        versions=[version("1.5")],
        retrieved_at=T1,
    )
    assert run_id is not None
    assert result.inserted == 1
    assert counts(factory) == {**NOTHING, "runs": 1, "observations": 1}


def test_the_catalog_synced_ercot_and_fred_sources_are_blocked(
    factory: sessionmaker[Session],
) -> None:
    """The real catalog's ERCOT and FRED sources are refused once synced; EIA is not."""
    with factory() as s, s.begin():
        sync_catalog(s, load_catalog())
    with factory() as s:
        source_ids = {
            code: source_id for code, source_id in s.execute(select(Source.code, Source.id))
        }
    assert set(source_ids) == {"eia", "fred", "ercot"}

    for code in ("ercot", "fred"):
        tracker = IngestionRunTracker(
            factory, job=f"{code}.observations", source_id=source_ids[code]
        )
        with pytest.raises(SourceIngestionBlockedError), tracker:
            pass
    assert counts(factory) == NOTHING

    with IngestionRunTracker(factory, job="eia.observations", source_id=source_ids["eia"]) as run:
        pass
    assert run.status is not None
    assert run.status.value == "succeeded"
    assert counts(factory) == {**NOTHING, "runs": 1}


def test_the_fred_source_is_refused_on_every_write_path(factory: sessionmaker[Session]) -> None:
    """Raw payload row and both observation writers refuse the synced FRED source."""
    with factory() as s, s.begin():
        sync_catalog(s, load_catalog())
    with factory() as s:
        fred_id = s.scalar(select(Source.id).where(Source.code == "fred"))
        series_id = s.scalar(
            select(Series.id).where(Series.code == "ust_10y", Series.source_id == fred_id)
        )
    assert fred_id is not None and series_id is not None
    with factory() as s, s.begin():
        run = IngestionRun(job="test.unrelated", started_at=T1 - timedelta(hours=1))
        s.add(run)  # a run with no source is allowed
        s.flush()
        run_id = run.id

    with pytest.raises(SourceIngestionBlockedError), factory() as s, s.begin():
        record_raw_payload(
            s,
            ingestion_run_id=run_id,
            source_id=fred_id,
            result=FetchResult(
                content=b"{}",  # synthetic
                status_code=200,
                content_type="application/json",
                url="https://api.example.com/synthetic",
                retrieved_at=T1,
                elapsed_seconds=0.1,
                attempts=1,
            ),
            stored=StoredPayload("raw/fred/x/2026/11/synthetic.json.gz", "0" * 64, 2),
        )
    with pytest.raises(SourceIngestionBlockedError), factory() as s, s.begin():
        write_versions(
            s,
            series_id=series_id,
            ingestion_run_id=run_id,
            versions=[version("4.1")],
            retrieved_at=T1,
        )
    assert counts(factory) == {**NOTHING, "runs": 1}


# --- the FRED job and `marketpulse ingest fred`, against this database ----------------------


@pytest.fixture
def fred_world(factory: sessionmaker[Session], tmp_path: Path) -> tuple[Settings, FakeFred]:
    """A synced catalog, a synthetic key, and a simulated FRED API that would answer."""
    with factory() as s, s.begin():
        sync_catalog(s, load_catalog())
    fake = FakeFred()
    fake.series["DGS10"] = SimSeries(metadata_for(FRED_SPECS["ust_10y"]), dgs10_rows())
    settings = Settings(
        _env_file=None,
        fred_api_key=SecretStr(FRED_TEST_KEY),  # type: ignore[call-arg]
        marketpulse_data_dir=tmp_path,
    )
    return settings, fake


def test_the_fred_job_writes_nothing(
    factory: sessionmaker[Session], fred_world: tuple[Settings, FakeFred], tmp_path: Path
) -> None:
    settings, fake = fred_world
    with pytest.raises(SourceIngestionBlockedError):
        ingest_fred(
            settings=settings,
            session_factory=factory,
            series_codes=["ust_10y"],
            full=True,
            client_factory=fred_client_factory(fake),
        )
    assert fake.requests == []  # no request reached the (simulated) provider
    assert counts(factory) == NOTHING
    assert list(tmp_path.rglob("*")) == []  # no raw payload file


def test_the_fred_cli_writes_nothing(
    factory: sessionmaker[Session],
    fred_world: tuple[Settings, FakeFred],
    test_db_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pointed at this database with a key and a simulator, the command still refuses."""
    _, fake = fred_world
    monkeypatch.setenv("POSTGRES_DB", test_db_name)
    monkeypatch.setenv("MARKETPULSE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FRED_API_KEY", FRED_TEST_KEY)
    monkeypatch.setattr(fred_job, "default_client_factory", lambda _s: fred_client_factory(fake))
    get_settings.cache_clear()
    try:
        for args in (["ingest", "fred"], ["ingest", "fred", "--series", "ust_10y", "--full"]):
            assert main(args) == 1
            assert "ingestion of source 'fred' is blocked" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
    assert fake.requests == []
    assert counts(factory) == NOTHING
    assert list(tmp_path.rglob("*")) == []
