"""Command-line entry point: `uv run marketpulse <command>`."""

import argparse

from marketpulse import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="marketpulse", description="Market Pulse backend")
    parser.add_argument("--version", action="version", version=f"marketpulse {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the API server (development)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "marketpulse.api.main:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
