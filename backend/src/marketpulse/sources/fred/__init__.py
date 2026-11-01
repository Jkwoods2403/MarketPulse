"""FRED (Federal Reserve Bank of St. Louis) provider: parsing, periods, and real-time windows.

FRED API facts this package relies on (verified 2026-11-01 against the official docs and API):
- Observations carry a real-time period `[realtime_start, realtime_end]`, closed at both ends;
  `9999-12-31` means "still current".
- Missing values are represented as the string ".".
- One request may span at most 2,000 vintage dates, and return at most 100,000 rows.
- Observation dates are the first day of the period (monthly: YYYY-MM-01; quarterly: the first
  month of the quarter).
"""

from datetime import date

# FRED's open-ended real-time end. Also equal to `date.max`: never add a day to it.
REALTIME_OPEN_END = date(9999, 12, 31)
# FRED's earliest real-time start.
REALTIME_EARLIEST = date(1776, 7, 4)
MAX_VINTAGES_PER_REQUEST = 2000
MAX_ROWS_PER_REQUEST = 100_000
