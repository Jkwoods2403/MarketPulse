import { MetricCardSkeleton, MetricGrid } from "@/components/dashboard/metric-card";
import { Skeleton } from "@/components/data/state-message";

/** Shown while a module page loads: the same footprint as the dashboard, no values. */
export default function Loading() {
  return (
    <div className="space-y-3" role="status" aria-live="polite">
      <span className="sr-only">Loading dashboard…</span>
      <div className="space-y-2 border-b border-border pb-3">
        <Skeleton className="h-5 w-48" />
        <Skeleton className="h-3 w-full max-w-xl" />
      </div>
      <div className="border border-border">
        <div className="h-8 border-b border-border bg-panel" />
        <div className="p-3">
          <MetricGrid>
            {Array.from({ length: 6 }, (_, i) => (
              <MetricCardSkeleton key={i} />
            ))}
          </MetricGrid>
        </div>
      </div>
    </div>
  );
}
