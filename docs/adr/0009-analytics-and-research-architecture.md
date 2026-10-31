# ADR-0009: Analytics and research architecture

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

Market Pulse is also a quantitative research project. The analytics system must support
historical research, correlation analysis, backtesting, and machine learning, while avoiding
common errors: look-ahead bias, data leakage, overfitting, misleading statistics on non-stationary
series, and presenting correlation as causation.

## Decision

### Layers

1. **Exploration:** Jupyter notebooks in `research/`. Never imported by application code.
2. **Library:** `marketpulse.analytics`, tested Python modules:
   - `data`: point-in-time data access. Given an "as-of" date, returns only the observations and
     vintages known at that time (ADR-0007).
   - `features`: transformations (returns, log differences, rolling statistics, calendar and
     seasonal features), each documented and unit-tested.
   - `stats`: correlation (Pearson and Spearman, rolling), stationarity tests, lead/lag
     cross-correlation, Granger-style tests, regression diagnostics.
   - `backtest`: walk-forward evaluation framework with explicit train/validation/test splits by time.
   - `models`: baselines first (naive/persistence, seasonal naive, moving average, linear), then
     statistical and ML models (e.g. ARIMA-family, gradient boosting).
   - `evaluation`: metrics (MAE, RMSE, MAPE/sMAPE where meaningful, directional accuracy,
     calibration for probabilistic forecasts) and comparison against baselines.
3. **Results:** stored in the `analytics` schema with full reproducibility metadata:
   - model and parameters
   - code version (git commit)
   - data as-of date
   - training and test windows
   - metrics

### Rules

- **Point-in-time correctness:** backtests use vintage-aware data access. No future information
  enters training or features.
- **Every model is compared with simple baselines.** A model that does not beat its baseline
  out-of-sample is reported as such.
- **Walk-forward / time-series cross-validation only.** No random shuffling of time-series data.
- **Correlation is never presented as causation.** Relationship records store the statistical
  evidence (method, window, sample size, statistic, p-value or confidence interval) separately from
  any proposed causal mechanism. The UI labels the two differently.
- **Stationarity awareness:** correlations are computed on appropriate transformations (returns or
  differences) by default. Results on levels are labeled with a warning.
- **Multiple-testing awareness:** exploratory scans across many pairs report how many were tested,
  and apply corrections where appropriate.
- **Uncertainty is always shown:** intervals, sample sizes, and limitations accompany results.
- **Model outputs are labeled as forecasts** and are never presented as trading recommendations.

## Consequences

- Research results are reproducible and auditable.
- Honest evaluation may show that many models do not beat baselines; that is a valid outcome.
- Point-in-time access is more complex than querying latest values, but is necessary for valid
  backtests.
