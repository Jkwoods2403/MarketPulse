import { Suspense } from "react";

import { ApiStatus, ApiStatusFallback } from "@/components/system/api-status";

const PLANNED_MODULES = [
  "Market Dashboard",
  "Energy Intelligence",
  "Company Intelligence",
  "Daily Briefing",
  "Market Journal",
  "Market Connections",
  "Analytics Lab",
  "Calendar & Watchlists",
] as const;

export default function Home() {
  return (
    <div className="flex flex-1 flex-col">
      <header className="flex h-10 items-center justify-between border-b border-border bg-panel px-4">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-sm font-semibold tracking-wider text-accent">
            MARKET PULSE
          </span>
          <span className="text-xs text-faint">Energy &amp; market intelligence</span>
        </div>
        <span className="font-mono text-[11px] uppercase tracking-wider text-faint">
          Milestone 0 · Foundation
        </span>
      </header>

      <main className="grid flex-1 grid-cols-1 gap-px bg-border p-px md:grid-cols-[2fr_1fr]">
        <section className="bg-canvas p-4">
          <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted">
            Market data
          </h2>
          <p className="max-w-prose text-muted">
            No market data is connected yet. Data ingestion from primary sources (EIA, FRED) begins
            in Milestone 1. Every value shown here will carry its source, timestamp, and freshness
            status.
          </p>
          <ul className="mt-4 grid grid-cols-2 gap-px bg-border sm:grid-cols-4">
            {PLANNED_MODULES.map((name) => (
              <li key={name} className="bg-panel px-3 py-2 text-xs text-faint">
                {name}
              </li>
            ))}
          </ul>
        </section>

        <aside className="bg-canvas p-4">
          <h2 className="mb-3 text-[11px] font-semibold uppercase tracking-wider text-muted">
            System · Backend API
          </h2>
          <Suspense fallback={<ApiStatusFallback />}>
            <ApiStatus />
          </Suspense>
        </aside>
      </main>
    </div>
  );
}
