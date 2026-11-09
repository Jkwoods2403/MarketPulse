import Link from "next/link";
import { Suspense } from "react";

import {
  ApiStatusIndicator,
  ApiStatusIndicatorFallback,
  EnvironmentBadge,
} from "@/components/system/api-status";

/** Terminal header: brand, data policy reminder, and backend status. */
export function TopBar() {
  return (
    <header className="flex h-10 shrink-0 items-center justify-between gap-4 border-b border-border bg-panel px-4">
      <div className="flex min-w-0 items-baseline gap-3">
        <Link
          href="/"
          className="font-mono text-sm font-semibold tracking-wider text-accent hover:text-fg"
        >
          MARKET PULSE
        </Link>
        <span className="hidden truncate text-xs text-faint sm:inline">
          Energy &amp; market intelligence
        </span>
      </div>
      <div className="flex shrink-0 items-center gap-4 font-mono text-[11px] uppercase tracking-wider">
        <Suspense fallback={null}>
          <EnvironmentBadge />
        </Suspense>
        <span className="text-status-delayed">
          <span className="hidden md:inline">Delayed &amp; historical data · not live</span>
          <span className="md:hidden">Delayed data</span>
          <span className="sr-only">: sources publish with a delay; no value here is a live quote.</span>
        </span>
        <Suspense fallback={<ApiStatusIndicatorFallback />}>
          <ApiStatusIndicator />
        </Suspense>
      </div>
    </header>
  );
}
