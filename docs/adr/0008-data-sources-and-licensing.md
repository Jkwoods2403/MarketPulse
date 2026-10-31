# ADR-0008: Data sources and licensing

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

The platform must use real data from reputable providers and never fabricate data. Costs should
stay as low as reasonably possible. Initially the application is for **personal use**, but the
repository is a **public portfolio project**, and a public deployment may follow. Market data
licensing differs sharply between personal/internal use and public display.

## Decision

### Through Milestone 4: free government and primary sources only

| Source | Use |
|---|---|
| EIA API v2 | Oil and gas spot prices, weekly petroleum and gas storage, production, LNG exports, STEO, hourly grid data |
| FRED / ALFRED | Treasury yields, rates, macroeconomic indicators, revision vintages |
| ERCOT Public API and archives | Settlement point prices, load, fuel mix, renewables, grid conditions |
| U.S. Treasury Fiscal Data | Official daily yield curve |
| BLS, BEA | Inflation, employment, GDP |
| NOAA / NWS | Weather observations, forecasts, degree days |
| SEC EDGAR | Filings and XBRL financial data |

**No paid market-data subscription will be purchased until there is a clear, documented need.**
That decision is expected at Milestone 5 (equities and companies) and will be recorded in a new ADR.

### Rules

1. **Document every source** in `docs/data-sources.md`: terms of use, rate limits, attribution
   requirements, and redistribution rights. Re-verify terms when a connector is built; do not rely
   on remembered limits.
2. **Licensing is tracked per source** in `ref.sources` (e.g. `public_domain`, `attribution_required`,
   `personal_use_only`, `no_redistribution`), so a future public deployment can automatically
   exclude or restrict data that may not be redistributed.
3. **Some FRED series carry third-party copyright** (e.g. S&P and ICE indices). These are flagged
   and treated as restricted.
4. **Unofficial scraping** (for example the Yahoo Finance `yfinance` library) is not used as a
   data source. It conflicts with provider terms and with the reliability priority.
5. **Known gaps are shown, not filled.** Real-time futures (NYMEX/ICE), official index levels
   (S&P, Nasdaq), and international gas/LNG benchmarks (JKM, TTF) require paid licenses. Until
   licensed, the UI shows them as unavailable, or shows clearly labeled proxies (e.g. "SPY ETF",
   not "S&P 500").
6. **News:** store only headline, link, publisher, timestamp, and a short excerpt. Full articles
   are never republished.
7. **Secrets:** all API keys live in environment variables; `.env` files are gitignored, and
   `.env.example` lists variable names only.

## Consequences

- Operating cost of data is $0 through Milestone 4.
- Energy spot prices from EIA/FRED are delayed by one or more business days and are labeled as such.
- The data model and UI are ready for a public deployment that respects licensing.
- Intraday market coverage is limited until a paid provider is justified.
