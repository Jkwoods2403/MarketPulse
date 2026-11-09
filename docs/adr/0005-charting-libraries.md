# ADR-0005: Charting — TradingView Lightweight Charts and Apache ECharts

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

Market Pulse needs two different kinds of charts:

1. **Financial time series:** long price histories, candlesticks, crosshairs, smooth zoom and pan,
   multiple series, and thousands to hundreds of thousands of points.
2. **Quantitative analytics:** correlation heatmaps, scatter plots with regression lines,
   distributions, rolling statistics, and forecasts with uncertainty bands.

Recharts was originally proposed. It renders SVG, slows down on large series, and lacks native
financial chart types.

## Decision

- **TradingView Lightweight Charts** (Apache 2.0) for financial price and time-series charts.
  Its license requires attribution to TradingView; this will be displayed as the license requires.
- **Apache ECharts** (Apache 2.0, canvas rendering) for quantitative analytics charts.
- Both are wrapped in thin, project-owned React components (`frontend/src/components/charts/`)
  that apply the shared theme and always render source and freshness metadata.
- Recharts is not used.

## Consequences

- Each library is used for what it does best; good performance on large data sets.
- Two charting APIs to learn, hidden behind project-owned wrappers.
- The TradingView attribution requirement must be honored in the UI.

## Implementation status (2026-11-08)

This note records how the decision has been applied so far. The decision above is unchanged,
so no superseding ADR is needed.

- **TradingView Lightweight Charts (5.2.1, pinned) renders every chart in dashboard v1.**
  - The charts are historical line charts of the API's observations.
  - They are wrapped in project-owned components in `frontend/src/components/charts/`
    (`TimeSeriesChart`, `LineChartCanvas`, `RangeSelector`).
  - Periods missing at source are drawn as gaps, never interpolated.
  - Each chart has a text description for assistive technology.
- **Apache ECharts is not used yet.** The quantitative analytics charts it was chosen for
  (heatmaps, regressions, distributions, forecast bands) belong to the Analytics Lab (Milestone
  9), which has not started. It is not installed, and will be added when that work begins.
- **The TradingView attribution is honored.**
  - Every chart shows TradingView's attribution logo (`attributionLogo: true`, linking to
    tradingview.com).
  - Every chart footer shows the text of the library's NOTICE file verbatim
    ("TradingView Lightweight Charts™ Copyright (с) 2025 TradingView, Inc."), with links to
    tradingview.com and the Apache License 2.0.
  - The complete license and notice texts are in `frontend/THIRD_PARTY_NOTICES.md` and at
    `/notices` in the application.
