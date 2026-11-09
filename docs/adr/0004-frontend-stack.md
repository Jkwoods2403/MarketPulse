# ADR-0004: Frontend stack — Next.js, TypeScript, Tailwind CSS, shadcn/ui

- **Status:** Accepted
- **Date:** 2026-10-31

## Context

The interface must feel like a professional institutional market terminal: dense information,
clear hierarchy, a polished dark theme, interactive charts, and visible source/timestamp/freshness
on every panel. API keys must never reach the browser.

## Decision

- **Next.js (App Router) with TypeScript** in strict mode.
  - Next.js is used as a **presentation layer only**. Server components and route handlers may
    call the FastAPI backend, but no data-processing or provider logic lives in the frontend.
- **Tailwind CSS** with a dedicated set of design tokens (colors, spacing, typography, gain/loss
  colors) for a dark-first terminal theme.
- **shadcn/ui** (built on Radix primitives) for accessible components such as tables, tabs,
  dialogs, and command menus. Components are copied into the repo and themed, not used as an opaque
  dependency.
- **Generated API types:** TypeScript types are generated from FastAPI's OpenAPI schema so the
  frontend and backend contracts cannot drift silently.
- **A shared `DataStatus` convention:** every data panel renders its source, observation time,
  and a freshness/quality label (live, delayed, historical, estimated, forecast, unavailable).

## Alternatives considered

- **Vite + React SPA:** simpler, but loses server rendering and server-side API proxying. Next.js
  adds modest complexity for useful headroom.

## Consequences

- A consistent, themeable design system from the start.
- Two runtimes in development (Node and Python), accepted.

## Implementation status (2026-11-08)

This note records how the decision has been applied so far. The decision above is unchanged,
so no superseding ADR is needed.

- **Next.js, TypeScript (strict), and Tailwind CSS** are in use as decided. Design tokens are in
  `frontend/src/app/globals.css`. All text tokens meet WCAG AA contrast (4.5:1) on every
  surface.
- **shadcn/ui is deferred, not rejected.**
  - Dashboard v1 needs no dialog, dropdown, tabs, or command menu, so no Radix primitive would
    be used yet.
  - Where information depended on hover tooltips, it was made visible or announced to screen
    readers instead of adding a tooltip library.
  - Shared primitives (`Badge`, `Button`) live in `frontend/src/components/ui/`, following the
    shadcn/ui file convention.
  - shadcn/ui components will be added there, themed with the existing tokens, when a component
    first needs one of its primitives.
- **Generated API types are implemented.**
  - The backend writes its OpenAPI schema to `backend/openapi.json`
    (`uv run marketpulse openapi`), and a backend test fails if that file is stale.
  - openapi-typescript (a pinned development dependency) generates
    `frontend/src/lib/api-schema.ts` from it (`pnpm api:types`).
  - CI runs `pnpm api:check`, which fails if the types are out of date.
- **The shared data-status convention is implemented.** Panels show source, period, retrieval
  time, and freshness. Synthetic and calculated values are labeled separately.
