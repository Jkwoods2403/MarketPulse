"""Source-level ingestion block (ADR-0014) on every shared database write path.

A source row with code `ercot` exists here only to prove that nothing can be written for it:
no ingestion run, raw payload row, observation, or data-quality issue. A permitted control source
shows the block is specific to the blocked source. All rows are synthetic.
"""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

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
from marketpulse.ingestion.observation_writer import write_versions
from marketpulse.ingestion.raw_store import StoredPayload, record_raw_payload
from marketpulse.ingestion.runs import IngestionRunTracker, record_series_versions
from marketpulse.ingestion.snapshot_writer import write_snapshot
from marketpulse.ingestion.snapshots import SnapshotObservation, SnapshotScope
from marketpulse.sources.http import FetchResult

T1 = datetime(2026, 11, 7, 15, 0, tzinfo=UTC)
P = date(2026, 7, 1)


def make_source(session: Session, code: str) -> int:
    source = Source(
        code=code,
        name=f"{code} (synthetic)",
        homepage_url="https://example.invalid",
        license="no_redistribution",
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
