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

    return parser


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
