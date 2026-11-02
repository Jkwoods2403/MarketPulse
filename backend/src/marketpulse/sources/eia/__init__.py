"""EIA (U.S. Energy Information Administration) API v2: parsing, periods, and validation.

Facts this package relies on, verified against the live API on 2026-11-01 (see
backend/tests/fixtures/eia and docs/data-sources.md); several differ from EIA's documentation:
- Native routes (e.g. petroleum/pri/spt/data) return values and `total` as JSON strings; the
  series-ID route returns JSON numbers. Both are parsed exactly as decimals.
- A request returns at most 5,000 rows. Warnings are in a TOP-LEVEL `warnings` list of
  {"warning", "description"} objects: on native routes every page that does not hold all rows
  carries "incomplete return", and asking for more than 5,000 rows adds "parameter out of
  range: length". The series-ID route truncates with no warning at all. So completeness is
  always checked against `total`, and only the pagination notice on a non-final page is
  tolerated (see validate.py).
- An unknown series on a native route returns HTTP 200 with total "0" and no rows.
- Missing values are JSON null.
- Error bodies: {"error": "...", "code": 400|404}, or for an invalid key (HTTP 403)
  {"error": {"code": "API_KEY_INVALID", "message": "..."}}.
- Weekly petroleum and gas periods are week-ending Fridays (YYYY-MM-DD); monthly are YYYY-MM.
"""

MAX_ROWS_PER_REQUEST = 5000
