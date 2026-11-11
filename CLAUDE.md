# CLAUDE.md — Market Pulse

Permanent development instructions for this repository. Read this before making any change.

---

## 1. Mission

Market Pulse is an **energy-focused market intelligence platform** that combines financial market data, energy market fundamentals, economic indicators, company research, quantitative analytics, and machine learning.

It serves two purposes, and both carry equal weight:

1. **A practical daily platform** for monitoring financial markets, energy markets, electricity markets, companies, economic developments, and important news.
2. **A substantial Data Engineering and quantitative research project** that investigates relationships between markets and develops analytical models.

The finished product should feel like a **professional financial and energy-market intelligence terminal**, not a generic student dashboard.

---

## 2. Core Features

### 2.1 Daily Market Dashboard
- Live or latest available prices for commodities, equities, indices, and economic indicators.
- Energy coverage: WTI crude, Brent, natural gas (Henry Hub), LNG, electricity, and related energy markets.
- Major financial indices, Treasury yields, and relevant economic indicators.
- Clear daily price changes and historical charts.
- Every value shows its data timestamp and a delayed-data indicator where applicable.

### 2.2 Energy Intelligence
- Natural gas: supply, demand, storage, production, LNG exports.
- Crude oil: supply, demand, inventories, production.
- ERCOT electricity: demand, generation mix, prices, grid conditions.
- Weather and other factors that influence energy markets.

### 2.3 Company Intelligence
- Track companies across energy, oilfield services, utilities, power generation, semiconductors, and AI infrastructure.
- Initial coverage includes Halliburton, Caterpillar, NVIDIA, LCRA-related developments where relevant, and other strategically important companies.
- Track financial performance, earnings, news, and exposure to relevant market factors.
- Explain how market developments could affect individual companies.

### 2.4 Daily Market Briefing
- Summarize important developments across markets.
- For each item, cover: **what happened**, **why it matters**, **which markets may be affected**, and **what to watch next**.
- Provide original source links and publication timestamps.
- Clearly distinguish **confirmed facts** from **analytical interpretation**.

### 2.5 Market Journal
- Record market observations, hypotheses, supporting evidence, and expected outcomes.
- Support later evaluation of whether each hypothesis proved correct.
- Preserve historical entries; never silently overwrite or delete them. Edits should be tracked.

### 2.6 Market Connections
- Explore relationships among commodities, electricity, economic indicators, companies, and technology.
- Distinguish observed correlations from possible causal explanations.
- **Never present correlation as proof of causation.**

### 2.7 Analytics Lab
- Historical data analysis, statistical testing, visualizations, and forecasting.
- Implement machine learning models and compare them against simple baselines (e.g. naive/persistence, seasonal naive, moving average, linear regression).
- Evaluate with appropriate metrics and out-of-sample / walk-forward validation; avoid look-ahead bias and data leakage.
- Clearly communicate limitations and uncertainty.

### 2.8 Events Calendar and Watchlists
- Track economic releases, EIA reports, company earnings, and important energy-market events.
- Customizable watchlists and alerts.

---

## 3. Data Integrity Rules (non-negotiable)

These rules apply to code, UI, seed data, tests, documentation, and any text Claude writes.

- **Never fabricate** market data, news, prices, or financial information. If data is unavailable, show it as unavailable.
- **Every displayed market observation must identify its source and timestamp.**
- Clearly label data as **live**, **delayed**, **historical**, **estimated**, **forecast**, or **unavailable**.
- Store the source, retrieval time, and (where provided) the source's own observation/publication time for every data point.
- **Obtain data only directly from the organization that publishes it** (ADR-0015). Do not use aggregators or republishers as data providers. **FRED is retired** as a data provider: do not propose, fetch, ingest, or display FRED data. Historical FRED records and raw files are preserved and must not be deleted without a separately approved plan.
- Current source status: **EIA** is active and approved. **ERCOT** is on hold pending ERCOT's written permission (ADR-0014). Candidate direct primary sources for future work include the **Federal Reserve, U.S. Treasury, BLS, BEA, NOAA/NWS, SEC EDGAR, and company investor relations**; each is added only after its terms (including storage rights) are verified and recorded in `docs/data-sources.md` and the project owner approves it. A licensed market-data vendor remains a separate future decision (ADR-0008).
- Respect each source's terms of use and rate limits.
- Mock or placeholder data is allowed only in tests and local development fixtures, must be obviously labeled as such, and must never reach a production view.
- Do not make unsupported trading recommendations. Analysis may describe exposures, scenarios, and risks; it must not tell the user to buy or sell.
- Model outputs and forecasts must be labeled as such, with uncertainty and limitations stated.

---

## 4. Development Principles

- Prioritize **correctness, data quality, reliability, and maintainability** over feature count.
- Use a **modular architecture** with clearly separated responsibilities:
  - **Frontend** — presentation and interaction.
  - **Backend / API** — serving data and business logic.
  - **Data ingestion** — fetching, validating, normalizing, and storing source data.
  - **Analytics** — statistics, research, forecasting, and ML.
- **TypeScript** for the frontend. **Python** for data engineering and quantitative analysis where appropriate.
- **PostgreSQL** for persistent historical data when the architecture calls for it.
- Keep API keys and credentials in **environment variables**. **Never commit secrets.** Provide a `.env.example` with variable names only, and keep real `.env` files git-ignored.
- Include **error handling, data validation, logging, and tests** for every significant component.
- Ingestion must be idempotent where possible, validate schemas and value ranges, and handle missing data, revisions, and source outages explicitly.
- **Avoid unnecessary complexity and expensive infrastructure.** Prefer free/low-cost data sources and simple deployments until there is a demonstrated need for more.
- **Build incrementally**, testing each major component before moving on.

---

## 5. Interface Standards

- Polished **dark theme** as the primary design.
- High **information density** with clean visual hierarchy — terminal-like, not cluttered.
- **Interactive charts** (zoom, hover inspection, range selection) and useful comparisons.
- Consistent number formatting, units, and color conventions for gains/losses.
- Source and timestamp visible on every data panel; delayed/stale data visually flagged.
- Graceful empty, loading, and error states — never display a blank or misleading value.

---

## 6. Workflow

- **Explain significant architectural decisions before implementing them**, including trade-offs.
- **Do not make large or unrelated changes without approval.** Keep changes scoped to the task at hand.
- Keep code organized and documented.
- After each development milestone, explain:
  1. what was created,
  2. how it works,
  3. how to test it.
- Maintain Git history with **meaningful, descriptive commit messages**.
- **Keep `README.md` updated** as the project develops (setup, architecture, data sources, how to run and test).
- Update this file when permanent project conventions change.
