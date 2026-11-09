import type { ReactNode } from "react";

import { CalculatedSummary, type CalculatedState } from "@/components/dashboard/calculated-summary";
import { FreshnessBadge, SyntheticTag } from "@/components/data/freshness-badge";
import { Skeleton } from "@/components/data/state-message";
import type { SeriesSummary } from "@/lib/api";
import { cn } from "@/lib/cn";
import type { MetricDefinition } from "@/lib/dashboard";
import { formatCadence, formatPeriod, formatUtcTimestamp, formatValue } from "@/lib/format";

/**
 * One series' latest observed value, exactly as served by the API: value and units, the period
 * it describes, when Market Pulse retrieved it, its freshness, and its source. Values are
 * observed (published by the source). Calculated changes (`calculated`), when given, are shown
 * below them in a separate block labeled "Calculated".
 *
 * `series` is undefined when the API does not serve the code (not stored, or not permitted);
 * the card then says so instead of showing anything else.
 */
export function MetricCard({
  metric,
  series,
  calculated,
}: {
  metric: MetricDefinition;
  series: SeriesSummary | undefined;
  calculated?: CalculatedState;
}) {
  if (series === undefined) {
    return (
      <CardFrame label={metric.label} badges={<FreshnessBadge status="unavailable" />}>
        <ValueLine primary="—" secondary="Not available from the data API" />
        <Rows rows={[["Series", <span key="c" className="font-mono">{metric.code}</span>]]} />
      </CardFrame>
    );
  }

  const latest = series.latest;
  const freshness = series.freshness;
  const freshnessDetail =
    freshness.age_days === null
      ? `No observations stored · expected within ${freshness.max_staleness_days} days`
      : `Period ended ${freshness.age_days} days before ${freshness.reference_date} · expected within ${freshness.max_staleness_days} days`;

  const producer =
    series.original_source && series.original_source !== series.source.name
      ? ` · ${series.original_source}`
      : "";

  return (
    <CardFrame
      label={metric.label}
      badges={
        <>
          {series.synthetic ? <SyntheticTag /> : null}
          <FreshnessBadge status={freshness.status} title={freshnessDetail} />
        </>
      }
      synthetic={series.synthetic}
    >
      {latest ? (
        <ValueLine
          primary={formatValue(latest.value)}
          secondary={series.units}
          tag={series.synthetic ? "Synthetic" : "Observed"}
        />
      ) : (
        <ValueLine primary="—" secondary={`No observations stored · ${series.units}`} />
      )}
      <Rows
        rows={[
          ...(latest
            ? ([
                ["Period", formatPeriod(series.period_convention, latest.period_start, latest.period_end)],
                ["Retrieved", formatUtcTimestamp(latest.first_retrieved_at)],
                ["Confirmed", formatUtcTimestamp(latest.last_confirmed_at)],
              ] as const)
            : []),
          ["Cadence", formatCadence(series.frequency, series.period_convention)],
          ["Freshness", freshnessDetail],
          ["Source", `${series.source.name}${producer}`],
        ]}
      />
      {latest && calculated ? <CalculatedSummary calculated={calculated} /> : null}
      {metric.note ? <p className="text-[11px] leading-4 text-status-delayed">{metric.note}</p> : null}
    </CardFrame>
  );
}

function CardFrame({
  label,
  badges,
  synthetic = false,
  children,
}: {
  label: string;
  badges: ReactNode;
  synthetic?: boolean;
  children: ReactNode;
}) {
  return (
    <article
      className={cn(
        "flex min-w-0 flex-col gap-2 border bg-panel p-3",
        synthetic ? "border-dashed border-status-delayed/50" : "border-border",
      )}
    >
      <header className="flex items-start justify-between gap-2">
        <h3 className="text-xs font-medium leading-4 text-fg">{label}</h3>
        <div className="flex shrink-0 flex-wrap justify-end gap-1">{badges}</div>
      </header>
      {children}
    </article>
  );
}

function ValueLine({
  primary,
  secondary,
  tag,
}: {
  primary: string;
  secondary: string;
  tag?: string;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span
        className={cn(
          "font-mono text-xl font-semibold leading-7",
          primary === "—" ? "text-faint" : "text-fg",
        )}
      >
        {primary}
      </span>
      <span className="text-[11px] text-muted">{secondary}</span>
      {tag ? (
        <span className="font-mono text-[9px] uppercase tracking-wider text-faint">{tag}</span>
      ) : null}
    </div>
  );
}

function Rows({ rows }: { rows: ReadonlyArray<readonly [string, ReactNode]> }) {
  return (
    <dl className="mt-auto grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-[11px] leading-4">
      {rows.map(([term, value]) => (
        <div key={term} className="contents">
          <dt className="text-faint">{term}</dt>
          <dd className="min-w-0 truncate text-muted" title={typeof value === "string" ? value : undefined}>
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** Loading placeholder with the same footprint as a MetricCard. */
export function MetricCardSkeleton() {
  return (
    <div className="flex flex-col gap-2 border border-border bg-panel p-3" aria-hidden>
      <div className="flex justify-between gap-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-16" />
      </div>
      <Skeleton className="h-7 w-1/2" />
      <Skeleton className="h-3 w-full" />
      <Skeleton className="h-3 w-4/5" />
      <Skeleton className="h-3 w-3/5" />
    </div>
  );
}

export function MetricGrid({
  children,
  layout = "grid",
}: {
  children: ReactNode;
  layout?: "grid" | "stack";
}) {
  return (
    // Columns follow the width of the panel the grid sits in (container queries), not the
    // viewport, so cards stay readable in narrow side-by-side sections.
    <div className="@container">
      <div
        className={cn(
          "grid grid-cols-1 gap-2",
          layout === "grid" && "@xl:grid-cols-2 @4xl:grid-cols-3 @7xl:grid-cols-4",
        )}
      >
        {children}
      </div>
    </div>
  );
}
