# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

Market Pulse is a long-running project that will grow across data engineering, a web application,
and quantitative research. Decisions about data sources, licensing, storage, and module boundaries
have long-lived consequences and need to be understandable later — by the author, by reviewers of
the public portfolio repository, and by AI coding assistants working in the repo.

## Decision

Record significant architectural decisions as numbered Markdown ADRs in `docs/adr/`, using the
Context → Decision → Consequences format. Accepted ADRs are not edited in substance; they are
superseded by new ADRs.

A decision is "significant" if it affects module boundaries, the data model, data sources or
licensing, infrastructure or cost, or is expensive to reverse.

## Consequences

- The reasoning behind the architecture is preserved alongside the code.
- Changing a recorded decision requires an explicit new ADR, which discourages silent drift.
- Small overhead per decision, accepted as worthwhile.
