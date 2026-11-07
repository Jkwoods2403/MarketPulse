"""Command-line entry point: `uv run marketpulse <command>`."""

import argparse
from pathlib import Path
from typing import TYPE_CHECKING

from marketpulse import __version__

if TYPE_CHECKING:
    from marketpulse.ingestion.verify import VerifyReport
    from marketpulse.sources.eia.probe import EiaProbeReport


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marketpulse", description="Market Pulse backend")
    parser.add_argument("--version", action="version", version=f"marketpulse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API server (development)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes")

    db = sub.add_parser("db", help="Database migrations")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    upgrade = db_sub.add_parser("upgrade", help="Apply migrations (default: to latest)")
    upgrade.add_argument("revision", nargs="?", default="head")
    upgrade.add_argument(
        "--sql", action="store_true", help="Print the SQL instead of running it (no database)"
    )
    downgrade = db_sub.add_parser("downgrade", help="Revert migrations to a revision")
    downgrade.add_argument("revision", help="Target revision, e.g. -1 or base")
    db_sub.add_parser("current", help="Show the database's current revision")

    catalog = sub.add_parser("catalog", help="Series catalog")
    catalog_sub = catalog.add_subparsers(dest="catalog_command", required=True)
    catalog_sub.add_parser("validate", help="Validate the catalog files (no database needed)")
    sync = catalog_sub.add_parser("sync", help="Apply the catalog to ref.sources / ref.series")
    sync.add_argument(
        "--dry-run", action="store_true", help="Show what would change, then roll back"
    )

    ingest = sub.add_parser("ingest", help="Ingest data from a provider")
    ingest_sub = ingest.add_subparsers(dest="ingest_command", required=True)
    fred = ingest_sub.add_parser(
        "fred",
        help="Ingest FRED series (default: all catalog series)",
        description="Fetch FRED observations with full version history and store them. "
        "Series with no stored data get a full backfill; others are updated incrementally. "
        "Exit codes: 0 all succeeded, 1 any series failed, 2 some series partial.",
    )
    fred.add_argument(
        "--series",
        nargs="+",
        metavar="CODE",
        help="Catalog series codes to ingest (e.g. ust_10y real_gdp); default: all FRED series",
    )
    fred.add_argument(
        "--full",
        action="store_true",
        help="Re-fetch the complete history even if data is already stored",
    )
    eia = ingest_sub.add_parser(
        "eia",
        help="Ingest EIA series as first-seen snapshots (default: all permitted catalog series)",
        description="Fetch EIA observations and record them as first-seen snapshots (EIA "
        "publishes no vintages; version times are when Market Pulse retrieved the data). "
        "Series with no stored data get a full snapshot; others an incremental one "
        "(lookback 90 days / 26 weeks / 26 months). "
        "Series licensed personal_use_only are never ingested: requesting one is an error "
        "(exit 1), and a default run skips them. "
        "Exit codes: 0 all succeeded, 1 any series failed, 2 some series partial "
        "(snapshot rejected, nothing changed).",
    )
    eia.add_argument(
        "--series",
        nargs="+",
        metavar="CODE",
        help="Catalog series codes to ingest (e.g. us_lng_exports l48_gas_storage); "
        "default: all EIA series whose license permits ingestion",
    )
    eia.add_argument(
        "--full",
        action="store_true",
        help="Take a full-history snapshot (reconciliation) even if data is already stored",
    )
    ingest_sub.add_parser(
        "ercot",
        help="ERCOT ingestion (blocked: licensing clarification pending, ADR-0014)",
        description="ERCOT ingestion is blocked until ERCOT answers licensing ticket SS0007218 "
        "and the project owner's decision is recorded (ADR-0014). This command always exits 1 "
        "before any network request, database connection, ingestion run, or file. It has no "
        "options; the block cannot be lifted from the command line or the environment.",
    )

    verify = sub.add_parser(
        "verify",
        help="Check stored observations and lineage for integrity (read-only)",
        description="Run every integrity check on the stored data in one read-only "
        "transaction: version overlaps and gaps, current versions, runs that failed or never "
        "finished, EIA provenance and first-seen times, FRED vintage times, tracked issues, and "
        "credentials in stored text. Never changes the database or any file. "
        "With --replay, each replayed version is counted once, in the first state that "
        "applies: ERROR (a mismatch), UNVERIFIED (evidence missing, e.g. a raw file), "
        "UNSUPPORTED (its creation matched, but a later closure or confirmation by another run "
        "was not replayed), else VERIFIED (creation, provenance, and stored history matched). "
        "Exit codes, first that applies: 1 any error; 2 warnings, no errors; 3 no errors or "
        "warnings, but some replayed versions are UNVERIFIED or UNSUPPORTED; 0 nothing found "
        "(with --replay: every replayed version VERIFIED; without it, only the SQL checks ran).",
    )
    verify.add_argument(
        "--series",
        nargs="+",
        metavar="CODE",
        help="Limit series-level checks to these catalog codes (default: every series)",
    )
    verify.add_argument(
        "--replay",
        action="store_true",
        help="Also re-read the stored raw pages under MARKETPULSE_DATA_DIR (read-only, "
        "hash-verified) and compare them with the versions they produced",
    )

    probe = sub.add_parser("probe", help="Read-only live checks of a provider (stores nothing)")
    probe_sub = probe.add_subparsers(dest="probe_command", required=True)
    probe_eia = probe_sub.add_parser(
        "eia",
        help="Check the catalogued EIA series against the live API (read-only)",
        description="For each catalogued EIA series, check against the live API that the "
        "route still offers its frequency and date format, that the series-ID route maps to "
        "the catalogued route and facet, and that the first and last rows match the catalogued "
        "metadata; report the row count, first and last periods, and staleness. Stores "
        "nothing: no database access, no ingestion runs, no raw responses kept. Never shows "
        "data values. Up to 4 requests per series (route metadata once per route). "
        "Exit codes: 0 every series ok; 1 any series failed (or a configuration error); "
        "2 warnings only (a series is older than its max_staleness_days).",
    )
    probe_eia.add_argument(
        "--series",
        nargs="+",
        metavar="CODE",
        help="Catalog series codes to probe (e.g. l48_gas_storage); default: all EIA series",
    )

    return parser


def _print_eia_probe_report(report: "EiaProbeReport") -> None:
    header = (
        f"{'series':<24} {'license':<18} {'status':<6} {'rows':>7} {'first':<10} {'last':<10} "
        f"{'age':>5} {'max':>4}"
    )
    print(header)
    print("-" * len(header))
    for s in report.series:
        rows = "-" if s.total is None else f"{s.total:,}"
        age = "-" if s.age_days is None else f"{s.age_days}d"
        print(
            f"{s.code:<24} {s.license:<18} {s.status:<6} {rows:>7} {s.first_period or '-':<10} "
            f"{s.last_period or '-':<10} {age:>5} {s.max_staleness_days:>4}"
        )
    for s in report.series:
        for label, messages in (("FAIL", s.problems), ("WARN", s.warnings)):
            for message in messages:
                text = message.removeprefix(f"{s.code}: ").replace("\n", "\n      ")
                print(f"  {label} {s.code}: {text}")
    restricted = [s.code for s in report.series if s.restricted]
    print(
        f"\nprobe eia ({report.probed_on.isoformat()}): {len(report.series)} series, "
        f"{report.count('ok')} ok, {report.count('warn')} warn, {report.count('fail')} fail; "
        f"{report.responses} responses received and discarded"
    )
    if restricted:
        print(
            f"restricted (licensed for personal use only; values never shown or stored by the "
            f"probe): {', '.join(restricted)}"
        )
    print(
        "read-only: no database access, no ingestion runs, no raw responses kept, no values shown"
    )


def _print_verify_report(report: "VerifyReport", data_dir: Path | None) -> None:
    def count(value: int | None) -> str:
        return "-" if value is None else f"{value:,}"

    header = (
        f"{'check':<32} {'status':<11} {'errors':>7} {'warnings':>8} {'unverified':>10} "
        f"{'unsupported':>11} {'verified':>8}"
    )
    print(header)
    print("-" * len(header))
    for c in report.checks:
        replay = c.verified is not None  # only replay checks count items in every state
        print(
            f"{c.name:<32} {c.status:<11} {c.errors:>7,} {c.warnings:>8,} "
            f"{count(c.unverified if replay else None):>10} "
            f"{count(c.unsupported if replay else None):>11} {count(c.verified):>8}"
        )
    for c in report.checks:
        if c.samples:
            print(
                f"\n{c.name}: {c.description} ({len(c.samples)} of {c.findings:,} finding(s) shown)"
            )
            for sample in c.samples:
                print(f"  {sample}")

    scope = ", ".join(report.series_codes) if report.series_codes else "all series"
    print(
        f"\nverify ({scope}): {report.series} series, {report.versions:,} versions, "
        f"{report.raw_payloads:,} raw payloads; {len(report.checks)} checks, "
        f"{report.errors:,} errors, {report.warnings:,} warnings"
    )
    if report.replayed:
        replay_errors = sum(c.errors for c in report.checks if c.verified is not None)
        print(
            f"replay (versions): VERIFIED {report.verified:,}, ERROR {replay_errors:,}, "
            f"UNVERIFIED {report.unverified:,}, UNSUPPORTED {report.unsupported:,}"
        )
        if report.unsupported:
            print(
                "  UNSUPPORTED: creation and source page matched, but a later closure or "
                "confirmation by another run was not replayed; not counted as verified"
            )
        if data_dir is not None and not data_dir.is_dir():
            print(f"note: raw-payload directory {data_dir} does not exist")
    else:
        print("replay: not run (add --replay to compare versions with their stored raw pages)")
    print("read-only; nothing was changed")


def _verify_command(series: list[str] | None, *, replay: bool = False) -> int:
    from marketpulse.core.config import get_settings
    from marketpulse.db.session import make_engine, make_session_factory
    from marketpulse.ingestion.raw_store import RawPayloadStore
    from marketpulse.ingestion.verify import VerifyConfigError, verify

    settings = get_settings()
    secrets = [
        key.get_secret_value()
        for key in (settings.fred_api_key, settings.eia_api_key, settings.postgres_password)
        if key is not None
    ]
    # Read-only: RawPayloadStore only resolves the path here and reads files during replay.
    raw_store = RawPayloadStore(settings.marketpulse_data_dir) if replay else None
    engine = make_engine(settings)
    try:
        report = verify(
            make_session_factory(engine),
            series_codes=series,
            secrets=secrets,
            raw_store=raw_store,
        )
    except VerifyConfigError as exc:
        print(f"error: {exc}")
        return 1
    finally:
        engine.dispose()

    _print_verify_report(report, raw_store.data_dir if raw_store else None)
    return report.exit_code


def _probe_eia_command(series: list[str] | None) -> int:
    from marketpulse.core.config import get_settings
    from marketpulse.sources.catalog import CatalogError, load_catalog
    from marketpulse.sources.eia.client import EiaClient, EiaConfigError
    from marketpulse.sources.eia.probe import EiaProbeConfigError, probe_eia
    from marketpulse.sources.http import FetchResult

    settings = get_settings()
    key = settings.eia_api_key
    if key is None or not key.get_secret_value().strip():
        print("error: EIA_API_KEY is not set or is blank (set it in .env)")
        return 1
    received = 0

    def discard(dataset: str, result: FetchResult) -> None:
        nonlocal received
        received += 1  # counted, then dropped: nothing is kept

    try:
        catalog = load_catalog()
        with EiaClient.from_settings(settings, on_response=discard) as client:
            report = probe_eia(client, catalog, series_codes=series, responses=lambda: received)
    except (EiaProbeConfigError, EiaConfigError, CatalogError) as exc:
        print(f"error: {exc}")
        return 1
    _print_eia_probe_report(report)
    return report.exit_code


def _ingest_fred_command(series: list[str] | None, *, full: bool) -> int:
    from marketpulse.core.config import get_settings
    from marketpulse.db.session import make_engine, make_session_factory
    from marketpulse.ingestion.fred import FredIngestionConfigError, ingest_fred
    from marketpulse.sources.catalog import CatalogError

    settings = get_settings()
    engine = make_engine(settings)
    try:
        summary = ingest_fred(
            settings=settings,
            session_factory=make_session_factory(engine),
            series_codes=series,
            full=full,
        )
    except (FredIngestionConfigError, CatalogError) as exc:
        print(f"error: {exc}")
        return 1
    finally:
        engine.dispose()

    header = (
        f"{'series':<22} {'mode':<11} {'status':<9} {'fetched':>8} {'inserted':>8} "
        f"{'revised':>7} {'unchanged':>9} {'issues':>6}  run"
    )
    print(header)
    print("-" * len(header))
    for o in summary.outcomes:
        print(
            f"{o.code:<22} {o.mode:<11} {o.status:<9} {o.fetched:>8} {o.inserted:>8} "
            f"{o.revised:>7} {o.unchanged:>9} {o.issues:>6}  {o.run_id or '-'}"
        )
    for o in summary.outcomes:
        if o.error:
            print(f"  {o.code}: {o.error}")
    totals = {
        s: sum(o.status == s for o in summary.outcomes) for s in ("succeeded", "partial", "failed")
    }
    print(
        f"{len(summary.outcomes)} series: {totals['succeeded']} succeeded, "
        f"{totals['partial']} partial, {totals['failed']} failed"
    )
    return summary.exit_code


def _ingest_eia_command(series: list[str] | None, *, full: bool) -> int:
    from marketpulse.core.config import get_settings
    from marketpulse.db.session import make_engine, make_session_factory
    from marketpulse.ingestion.eia import UNFINALIZED, EiaIngestionConfigError, ingest_eia
    from marketpulse.sources.catalog import CatalogError
    from marketpulse.sources.eia.client import EiaConfigError

    settings = get_settings()
    engine = make_engine(settings)
    try:
        summary = ingest_eia(
            settings=settings,
            session_factory=make_session_factory(engine),
            series_codes=series,
            full=full,
        )
    except (EiaIngestionConfigError, EiaConfigError, CatalogError) as exc:
        print(f"error: {exc}")
        return 1
    finally:
        engine.dispose()

    for code, license_ in summary.skipped_restricted.items():
        print(
            f"skipped {code}: licensed {license_.value}; not ingested without appropriate "
            "licensing (see docs/data-sources.md)"
        )
    header = (
        f"{'series':<24} {'mode':<11} {'status':<11} {'fetched':>8} {'inserted':>8} "
        f"{'revised':>7} {'unchanged':>9} {'vanished':>8} {'issues':>6}  run"
    )
    print(header)
    print("-" * len(header))
    for o in summary.outcomes:
        vanished = "-" if o.vanished is None else str(o.vanished)  # "-": not determined
        status = o.status.upper() if o.status == UNFINALIZED else o.status  # stands out
        print(
            f"{o.code:<24} {o.mode:<11} {status:<11} {o.fetched:>8} {o.inserted:>8} "
            f"{o.revised:>7} {o.unchanged:>9} {vanished:>8} {o.issues:>6}  {o.run_id or '-'}"
        )
    for o in summary.outcomes:
        if o.rejected:
            print(f"  {o.code}: snapshot rejected; no observations changed (see run issues)")
        if o.error:
            print(f"  {o.code}: {o.error}")
        if o.status == UNFINALIZED:
            committed = (
                "its observation changes WERE COMMITTED and are kept"
                if o.observations_committed
                else "no observations were changed"
            )
            print(
                f"  {o.code}: UNFINALIZED: run {o.run_id} could not record its outcome and still "
                f"says 'running'; {committed}: {o.finalization_error}"
            )
    statuses = ("succeeded", "partial", "failed", UNFINALIZED)
    totals = {s: sum(o.status == s for o in summary.outcomes) for s in statuses}
    print(
        f"{len(summary.outcomes)} series: {totals['succeeded']} succeeded, "
        f"{totals['partial']} partial, {totals['failed']} failed"
        + (f", {totals[UNFINALIZED]} unfinalized" if totals[UNFINALIZED] else "")
    )
    return summary.exit_code


def _ingest_ercot_command() -> int:
    """Refuse ERCOT ingestion (ADR-0014) before settings, a database, or a client are created."""
    from marketpulse.core.source_access import (
        SourceIngestionBlockedError,
        require_source_ingestion_permitted,
    )

    try:
        require_source_ingestion_permitted("ercot")
    except SourceIngestionBlockedError as exc:
        print(f"error: {exc}")
        return 1
    # Only reachable if ERCOT were removed from the block list without a job being built.
    print("error: no ERCOT ingestion job is implemented")
    return 1


def _catalog_command(command: str, *, dry_run: bool = False) -> int:
    from marketpulse.sources.catalog import CatalogError, load_catalog

    try:
        catalog = load_catalog()
    except CatalogError as exc:
        print(exc)
        return 1

    if command == "validate":
        counts = {code: len(catalog.series_for(code)) for code in catalog.sources}
        summary = ", ".join(f"{code}: {n}" for code, n in counts.items())
        print(
            f"Catalog OK: {len(catalog.sources)} sources, {len(catalog.series)} series ({summary})"
        )
        return 0

    from marketpulse.core.config import get_settings
    from marketpulse.db.session import make_engine, make_session_factory
    from marketpulse.ingestion.catalog_sync import CatalogSyncError, sync_catalog

    engine = make_engine(get_settings())
    try:
        with make_session_factory(engine)() as session:
            try:
                report = sync_catalog(session, catalog)
            except CatalogSyncError as exc:
                session.rollback()
                print(exc)
                return 1
            if dry_run:
                session.rollback()
            else:
                session.commit()
    finally:
        engine.dispose()

    prefix = "[dry run] would have " if dry_run else ""
    print(
        f"{prefix}sources: {len(report.sources_created)} created, "
        f"{len(report.sources_updated)} updated, {len(report.sources_unchanged)} unchanged"
    )
    print(
        f"{prefix}series: {len(report.series_created)} created, "
        f"{len(report.series_updated)} updated, {len(report.series_unchanged)} unchanged, "
        f"{len(report.series_deactivated)} deactivated"
    )
    for code, fields in report.series_updated.items():
        print(f"  updated {code}: {', '.join(fields)}")
    for code in report.series_deactivated:
        print(f"  deactivated {code} (no longer in catalog)")
    for code in report.sources_not_in_catalog:
        print(f"  warning: source {code!r} is in the database but not in the catalog")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "marketpulse.api.main:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    elif args.command == "db":
        from marketpulse.db import migrations

        if args.db_command == "upgrade":
            migrations.upgrade(revision=args.revision, sql=args.sql)
        elif args.db_command == "downgrade":
            migrations.downgrade(revision=args.revision)
        elif args.db_command == "current":
            migrations.current()
    elif args.command == "catalog":
        return _catalog_command(args.catalog_command, dry_run=getattr(args, "dry_run", False))
    elif args.command == "verify":
        return _verify_command(args.series, replay=args.replay)
    elif args.command == "probe" and args.probe_command == "eia":
        return _probe_eia_command(args.series)
    elif args.command == "ingest" and args.ingest_command == "fred":
        return _ingest_fred_command(args.series, full=args.full)
    elif args.command == "ingest" and args.ingest_command == "eia":
        return _ingest_eia_command(args.series, full=args.full)
    elif args.command == "ingest" and args.ingest_command == "ercot":
        return _ingest_ercot_command()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
