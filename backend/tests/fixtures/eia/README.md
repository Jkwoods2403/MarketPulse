# EIA test fixtures: real API v2 responses (public-domain series only)

**These files are real, unmodified EIA API v2 response bodies**, recorded on 2026-11-01 with
`backend/scripts/record_eia_fixtures.py`. Requests were kept small (short date windows or a few
rows), so no trimming was applied afterwards. The one large response
(`native_overlimit_silent_truncation.json.gz`, 1.5 MB) is stored gzip-compressed, which is
lossless; its SHA-256 in `manifest.json` is of the uncompressed body. A test verifies every hash.

**No Refinitiv-sourced data is included.** WTI, Brent, and Henry Hub spot prices are Refinitiv
(LSEG) data republished by EIA and classified `personal_use_only`; they are never recorded here,
and a test fails if any fixture mentions them. Behavior observed only in those series (for
example the JSON `null` value Henry Hub reports for 2018-01-05) is reproduced in tests with
**synthetic, explicitly labelled** data instead.

The invalid-key fixture was produced with a made-up key, never the real one.

| File | What it captures |
|---|---|
| `metadata_petroleum_sum_sndw.json` | Route metadata: frequencies `weekly` *and* `four-week-average`, facets, data columns |
| `seriesid_wcestus1_sort_asc_ignored.json` | Series-ID route: values and total are JSON numbers; requested ascending sort is ignored (echo shows `desc`); echo reveals the native route |
| `seriesid_wcestus1_start_end_ignored.json` | Series-ID route ignores `start`/`end` (echo shows `null`, total is the full history) |
| `native_wcestus1_2026_jan_feb.json` | Native route weekly window: values and total are strings, ascending, weeks end on Friday |
| `native_wpuleus3_2026_jan_feb.json` | Native route decimal strings (`"94.7"`) |
| `native_n9133us2_2025.json` | Native route monthly window: `YYYY-MM` periods |
| `native_storage_page1.json`, `native_storage_page2.json` | Native route offset paging (3 rows per page); storage history starts 2010-01-01 |
| `native_overlimit_silent_truncation.json.gz` | `length=6000` with 65,642 rows available: HTTP 200, 5,000 rows, **no warning** (contrary to EIA's documentation) |
| `native_unknown_facet_empty.json` | Unknown series on the native route: HTTP 200, total `"0"`, no rows (not an error) |
| `error_unknown_series_id.json` | HTTP 404 `{"error": ..., "code": 404}` |
| `error_invalid_data_column.json`, `error_invalid_frequency.json` | HTTP 400 `{"error": ..., "code": 400}` |
| `error_invalid_api_key.json` | HTTP 403 `{"error": {"code": "API_KEY_INVALID", "message": ...}}` (a different shape) |

**Source and attribution:** U.S. Energy Information Administration (EIA). EIA data are U.S.
government publications in the public domain; EIA's API Terms of Service ask users to identify
EIA as the source and not to modify content while attributing it to EIA.

Values reflect EIA as of the retrieval date and may differ from later downloads as data are
revised. Never use these files as a data source; they are test inputs only.
