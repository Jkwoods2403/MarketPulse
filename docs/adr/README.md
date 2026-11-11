# Architecture Decision Records

This directory records significant architectural decisions for Market Pulse, using a lightweight
[ADR](https://adr.github.io/) format: **Context → Decision → Consequences**.

ADRs are immutable once accepted. To change a decision, add a new ADR that supersedes the old one and
update the old ADR's status to `Superseded by ADR-NNNN`.

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-system-architecture-and-module-boundaries.md) | System architecture and module boundaries | Accepted |
| [0003](0003-monorepo-and-project-structure.md) | Monorepo and project structure | Accepted |
| [0004](0004-frontend-stack.md) | Frontend stack: Next.js, TypeScript, Tailwind, shadcn/ui | Accepted |
| [0005](0005-charting-libraries.md) | Charting: Lightweight Charts and Apache ECharts | Accepted |
| [0006](0006-backend-and-data-pipeline-stack.md) | Backend and data pipeline stack: Python, FastAPI, scheduled ingestion | Accepted |
| [0007](0007-postgresql-and-data-lineage-model.md) | PostgreSQL and the data lineage model | Accepted |
| [0008](0008-data-sources-and-licensing.md) | Data sources and licensing | Accepted; amended by ADR-0015 |
| [0009](0009-analytics-and-research-architecture.md) | Analytics and research architecture | Accepted |
| [0010](0010-cost-and-deployment-strategy.md) | Cost and deployment strategy | Accepted |
| [0011](0011-first-seen-snapshot-versioning.md) | First-seen snapshot versioning for providers without vintages | Accepted |
| [0012](0012-ercot-storage-and-timestamp-architecture.md) | ERCOT storage and timestamp architecture | Proposed |
| [0013](0013-ercot-market-time-and-daylight-saving.md) | ERCOT market time and daylight saving | Proposed |
| [0014](0014-ercot-licensing-and-ingestion-gate.md) | ERCOT licensing classification and the pending-clarification ingestion gate | Proposed |
| [0015](0015-direct-source-data-only.md) | Direct-source data only; FRED retired as a data provider | Accepted |

## Template

```markdown
# ADR-NNNN: Title

- **Status:** Proposed | Accepted | Superseded by ADR-NNNN
- **Date:** YYYY-MM-DD

## Context
## Decision
## Consequences
```
