# FRED test fixtures: real API responses

**These files are real, unmodified FRED API response bodies**, recorded on 2026-11-01 with
`backend/scripts/record_fred_fixtures.py`. They are deliberately small: each request was limited
to a few observation dates or rows so the bodies are a few hundred bytes, and no trimming was
applied afterwards. `manifest.json` lists, for each file, the purpose, the request URL (API key
redacted), HTTP status, retrieval time, and SHA-256 of the body; a test verifies the hashes.

They exist so the parser and client are tested against FRED's *actual* formats (field names,
paging fields, `"."` for missing values, real-time windows, error bodies) rather than formats
assumed from documentation. Edge cases that cannot be captured from the live API (conflicting
periods, paging failures, retries) use clearly labelled synthetic data in the tests.

| File | What it captures |
|---|---|
| `series_gdpc1.json`, `series_dgs10.json` | Series metadata (quarterly SAAR; daily NSA) |
| `vintagedates_gdpc1_limit5.json` | Vintage dates with real paging fields (count 420, 5 returned) |
| `observations_gdpc1_2024q1q2_page{1,2}.json` | Revision history across two pages (11 rows, offset paging) |
| `observations_cpiaucsl_2026aug_sep.json` | Monthly dates (first of month); no revisions in this range |
| `observations_dgs10_holidays_window.json` | Daily values with holiday `"."` values, inside a real-time window |
| `error_invalid_series_id.json` | HTTP 400 for a malformed series ID |
| `error_nonexistent_series.json` | HTTP 400 for a well-formed series ID that does not exist |
| `error_too_many_vintages.json` | HTTP 400 when a request spans more than 2,000 vintage dates |

**Source and attribution:** FRED, Federal Reserve Bank of St. Louis. Original producers: U.S.
Bureau of Economic Analysis (GDPC1), U.S. Bureau of Labor Statistics (CPIAUCSL), Board of
Governors of the Federal Reserve System (DGS10). These are U.S. government data and none of
these series carries a third-party copyright notice on FRED.

Values reflect FRED as of the retrieval date and will differ from later downloads as data are
revised. Never use these files as a data source; they are test inputs only.
