"""Market Pulse: energy-focused market intelligence platform (backend package).

Module boundaries are enforced by import-linter (see docs/adr/0002):

    core       configuration, logging, shared utilities (depends on nothing internal)
    db         database models and sessions             (core)
    sources    one client per external data provider    (core)
    ingestion  fetch -> validate -> normalize -> store  (core, db, sources)
    analytics  statistics, backtesting, models          (core, db)
    content    news aggregation and briefings           (core, db)
    api        FastAPI application, read-only from db   (core, db, analytics)
"""

from importlib.metadata import version

__version__ = version("marketpulse")
