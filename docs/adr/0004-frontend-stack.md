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
