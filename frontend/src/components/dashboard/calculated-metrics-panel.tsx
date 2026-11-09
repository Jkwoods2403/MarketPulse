import { Suspense } from "react";

import { SyntheticTag } from "@/components/data/freshness-badge";
import { Skeleton, StateMessage } from "@/components/data/state-message";
import { Badge } from "@/components/ui/badge";
import { getMetrics, type CalculatedMetric, type ObservationRef, type SeriesMetrics } from "@/lib/api";
import type { MetricDefinition } from "@/lib/dashboard";
import { formatPeriod, formatUtcTimestamp, formatValue } from "@/lib/format";
import {
  barPosition,
  formatChange,
  formatPosition,
  headlineMetrics,
  provenanceLabel,
  rangeMetrics,
} from "@/lib/metric-format";

/**
 * Calculated metrics for a module page (GET /metrics): every change with the exact periods it
 * compares, five-year ranges with their minimum and maximum, the methodology, and the reasons
 * any metric is not calculated. All of it is derived by Market Pulse from current versions of
 * the source's observations, and labeled "Calculated", never "Observed".
 */
export function CalculatedMetricsPanel({ metrics }: { metrics: readonly MetricDefinition[] }) {
  return (
    <Suspense
      fallback={
        <div className="space-y-2" aria-busy="true">
          {metrics.map((m) => (
            <Skeleton key={m.code} className="h-16 w-full" />
          ))}
        </div>
      }
    >
      <LoadedPanel metrics={metrics} />
    </Suspense>
  );
}

async function LoadedPanel({ metrics }: { metrics: readonly MetricDefinition[] }) {
  const result = await getMetrics();
  if (result.state !== "ok") {
    return (
      <StateMessage tone="error" title="Calculated metrics could not be loaded">
        {result.reason}. No calculated values are shown.
      </StateMessage>
    );
  }
  const byCode = new Map(result.data.series.map((s) => [s.code, s]));
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 gap-2 xl:grid-cols-2">
        {metrics.map((def) => (
          <SeriesBlock key={def.code} label={def.label} series={byCode.get(def.code)} />
        ))}
      </div>
      <p className="text-[10px] leading-4 text-faint">
        Calculated by Market Pulse at {formatUtcTimestamp(result.data.generated_at)} from the
        current version of each observation (revised values replace earlier ones; no gaps are
        filled). These are derived values, not published by the source, and are not forecasts
        or recommendations.
      </p>
    </div>
  );
}

function SeriesBlock({ label, series }: { label: string; series: SeriesMetrics | undefined }) {
  if (series === undefined || series.current === null) {
    return (
      <article className="border border-border bg-panel p-3 text-[11px]">
        <h3 className="text-xs font-medium text-fg">{label}</h3>
        <p className="mt-1 text-faint">
          {series === undefined
            ? "Not available from the data API; nothing is calculated."
            : "No observations stored; nothing is calculated."}
        </p>
      </article>
    );
  }
  const convention = series.period_convention;
  const current = series.current;
  return (
    <article
      className={
        series.synthetic
          ? "min-w-0 border border-dashed border-status-delayed/50 bg-panel"
          : "min-w-0 border border-border bg-panel"
      }
    >
      <header className="flex items-start justify-between gap-2 border-b border-border px-3 py-2">
        <div className="min-w-0">
          <h3 className="truncate text-xs font-medium text-fg">{label}</h3>
          <p className="truncate text-[11px] text-muted">
            From {formatPeriod(convention, current.period_start, current.period_end)} ·{" "}
            <span className="font-mono">{current.value === null ? "—" : formatValue(current.value)}</span>{" "}
            {series.units}
          </p>
        </div>
        <div className="flex shrink-0 gap-1">
          {series.synthetic ? <SyntheticTag /> : null}
          <Badge
            tone="calculated"
            title="Derived by Market Pulse from the source's observations; not published by the source"
          >
            {provenanceLabel(series.synthetic)}
          </Badge>
        </div>
      </header>

      <table className="w-full text-[11px] leading-4">
        <caption className="sr-only">
          Calculated changes in {label}: metric, change, and the period compared against
        </caption>
        <tbody>
          {headlineMetrics(series).map((m) => (
            <ChangeRow key={m.kind} metric={m} convention={convention} />
          ))}
        </tbody>
      </table>

      {rangeMetrics(series).map((m) => (
        <RangeRow key={m.kind} metric={m} convention={convention} units={series.units} />
      ))}

      <details className="border-t border-border px-3 py-1.5 text-[10px] leading-4 text-faint">
        <summary className="cursor-pointer select-none hover:text-muted">Methodology & sources</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-4">
          {series.metrics.map((m) => (
            <li key={m.kind}>
              <span className="text-muted">{m.label}:</span> {m.methodology}
              {m.percent_note ? ` ${m.percent_note}` : ""}
            </li>
          ))}
          {series.not_calculated.map((n) => (
            <li key={`not-${n.kind}`}>
              <span className="text-muted">Not calculated ({n.kind.replaceAll("_", " ")}):</span>{" "}
              {n.reason}
            </li>
          ))}
          <li>
            Source series <span className="font-mono">{series.code}</span>:{" "}
            {series.source.attribution ?? series.source.name}. Every value used is listed with
            its period, so each result can be reproduced.
          </li>
        </ul>
      </details>
    </article>
  );
}

function periodOf(convention: string, obs: ObservationRef | null): string {
  return obs ? formatPeriod(convention, obs.period_start, obs.period_end) : "—";
}

function ChangeRow({ metric, convention }: { metric: CalculatedMetric; convention: string }) {
  const ok = metric.status === "ok";
  return (
    <tr className="border-b border-border/60 last:border-b-0" title={metric.methodology}>
      <th scope="row" className="w-32 px-3 py-1 text-left font-normal text-faint">
        {metric.label}
      </th>
      <td className={ok ? "px-2 py-1 font-mono text-fg" : "px-2 py-1 text-faint"}>{formatChange(metric)}</td>
      <td className="px-3 py-1 text-right text-faint">
        {metric.comparison
          ? `vs ${periodOf(convention, metric.comparison)}${
              metric.comparison.value === null ? " (missing at source)" : ` (${formatValue(metric.comparison.value)})`
            }`
          : (metric.reason ?? "")}
      </td>
    </tr>
  );
}

function RangeRow({
  metric,
  convention,
  units,
}: {
  metric: CalculatedMetric;
  convention: string;
  units: string;
}) {
  const position = barPosition(metric);
  const ok = metric.status === "ok" && metric.minimum !== null && metric.maximum !== null;
  return (
    <div className="space-y-1 border-t border-border px-3 py-2 text-[11px] leading-4" title={metric.methodology}>
      <div className="flex justify-between gap-2">
        <span className="text-faint">{metric.label}</span>
        <span className={ok ? "text-muted" : "text-faint"}>{formatPosition(metric)}</span>
      </div>
      {ok && metric.minimum && metric.maximum ? (
        <>
          <div className="relative h-2 rounded-sm bg-panel-raised" aria-hidden>
            <div className="absolute inset-y-0 left-0 right-0 rounded-sm border border-border-strong" />
            {position !== null ? (
              <div
                className={
                  metric.position === "within"
                    ? "absolute -top-0.5 h-3 w-0.5 bg-accent"
                    : "absolute -top-0.5 h-3 w-0.5 bg-status-delayed"
                }
                style={{ left: `calc(${position}% - 1px)` }}
              />
            ) : null}
          </div>
          <div className="flex justify-between gap-2 font-mono text-[10px] text-faint">
            <span title={periodOf(convention, metric.minimum)}>
              Min {metric.minimum.value === null ? "—" : formatValue(metric.minimum.value)} ·{" "}
              {periodOf(convention, metric.minimum)}
            </span>
            <span title={periodOf(convention, metric.maximum)} className="text-right">
              Max {metric.maximum.value === null ? "—" : formatValue(metric.maximum.value)} ·{" "}
              {periodOf(convention, metric.maximum)}
            </span>
          </div>
          {metric.kind === "seasonal_range" ? (
            <p className="text-[10px] text-faint">
              Compared periods ({units}):{" "}
              {metric.window
                .map((o) => `${periodOf(convention, o)} ${o.value === null ? "—" : formatValue(o.value)}`)
                .join(" · ")}
            </p>
          ) : null}
        </>
      ) : metric.reason ? (
        <p className="text-[10px] text-faint">{metric.reason}.</p>
      ) : null}
    </div>
  );
}
