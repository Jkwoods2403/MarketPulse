# research/

Exploratory analysis notebooks (Jupyter). See
[ADR-0009](../docs/adr/0009-analytics-and-research-architecture.md).

Rules:

- **Notebooks are never imported by application code.** Code that proves useful is promoted into
  `backend/src/marketpulse/analytics/` with tests.
- **Clear outputs before committing.** Outputs can embed licensed data or large payloads.
- Load data through `marketpulse.analytics` point-in-time access (from Milestone 1). Never paste
  or hard-code market values.
- State the data as-of date, the sample period, and the limitations at the top of each notebook.
