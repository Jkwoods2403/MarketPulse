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

    return parser


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
