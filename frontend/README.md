# Market Pulse frontend

Next.js (App Router) + TypeScript + Tailwind CSS. This is the presentation layer only: all data
comes from the FastAPI backend, which the Next.js server calls on the browser's behalf (ADR-0004).

```bash
pnpm install       # install locked dependencies
pnpm dev           # http://localhost:3000

pnpm lint          # ESLint (zero warnings allowed)
pnpm typecheck     # TypeScript strict type check
pnpm build         # production build
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `MARKETPULSE_API_URL` | `http://localhost:8000` | Backend URL, server-side only |

Next.js reads `.env*` files from this directory (`frontend/`), not the repository root. To
override the default, create `frontend/.env.local` (gitignored).

## Conventions

- Design tokens live in `src/app/globals.css` (`bg-panel`, `text-muted`, `text-up`, `text-down`,
  `text-status-*`). Use semantic tokens, not raw colors.
- Data that must be fresh on every request is fetched in server components wrapped in
  `<Suspense>`, with `connection()` so it is never prerendered (Next.js 16 Cache Components).
- See `AGENTS.md`: this Next.js version differs from older documentation. Check
  `node_modules/next/dist/docs/` before using unfamiliar APIs.
