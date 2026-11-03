"""Command-line entry point: `uv run marketpulse <command>`."""

import argparse

from marketpulse import __version__


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
        help="Ingest EIA series as first-seen snapshots (default: all catalog series)",
        description="Fetch EIA observations and record them as first-seen snapshots (EIA "
        "publishes no vintages; version times are when Market Pulse retrieved the data). "
        "Series with no stored data get a full snapshot; others an incremental one "
        "(lookback 90 days / 26 weeks / 26 months). "
        "Exit codes: 0 all succeeded, 1 any series failed, 2 some series partial "
        "(snapshot rejected, nothing changed).",
    )
    eia.add_argument(
        "--series",
        nargs="+",
        metavar="CODE",
        help="Catalog series codes to ingest (e.g. wti_spot l48_gas_storage); "
        "default: all EIA series",
    )
    eia.add_argument(
        "--full",
        action="store_true",
        help="Take a full-history snapshot (reconciliation) even if data is already stored",
    )

    return parser


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
    elif args.command == "ingest" and args.ingest_command == "fred":
        return _ingest_fred_command(args.series, full=args.full)
    elif args.command == "ingest" and args.ingest_command == "eia":
        return _ingest_eia_command(args.series, full=args.full)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
