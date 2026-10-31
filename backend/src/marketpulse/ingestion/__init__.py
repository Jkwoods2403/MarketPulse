"""Ingestion jobs: fetch -> save raw -> validate -> normalize -> idempotent upsert -> record run.

May import: core, db, sources.
"""
