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
