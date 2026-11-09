import { Suspense } from "react";

import { MetricCard, MetricCardSkeleton, MetricGrid } from "@/components/dashboard/metric-card";
import { StateMessage } from "@/components/data/state-message";
import type { CalculatedState } from "@/components/dashboard/calculated-summary";
import { getMetrics, getSeriesList, type ApiResult, type SeriesList } from "@/lib/api";
import type { MetricDefinition } from "@/lib/dashboard";

type Layout = "grid" | "stack";

/**
 * Metric cards for one dashboard section, loaded from GET /series, with their calculated
 * changes from GET /metrics. Each section has its own Suspense boundary and error state, so one
 * failing or slow section never blanks the page; if only /metrics fails, the observed values
 * are still shown and the cards say the calculated block could not be loaded.
 */
export function SeriesCards({
  metrics,
  layout = "grid",
}: {
  metrics: readonly MetricDefinition[];
  layout?: Layout;
}) {
  return (
    <Suspense
      fallback={
        <MetricGrid layout={layout}>
          {metrics.map((m) => (
            <MetricCardSkeleton key={m.code} />
          ))}
        </MetricGrid>
      }
    >
      <LoadedSeriesCards metrics={metrics} layout={layout} />
    </Suspense>
  );
}

async function LoadedSeriesCards({
  metrics,
  layout,
}: {
  metrics: readonly MetricDefinition[];
  layout: Layout;
}) {
  const [result, metricsResult] = await Promise.all([getSeriesList(), getMetrics()]);
  if (result.state !== "ok") return <SeriesLoadError result={result} />;

  const byCode = new Map(result.data.series.map((s) => [s.code, s]));
  const metricsByCode =
    metricsResult.state === "ok" ? new Map(metricsResult.data.series.map((s) => [s.code, s])) : null;
  const calculatedFor = (code: string): CalculatedState =>
    metricsByCode ? { state: "ok", series: metricsByCode.get(code) } : { state: "unavailable" };
  const served = metrics.filter((m) => byCode.has(m.code));
  return (
    <div className="space-y-2">
      {served.length === 0 ? (
        <StateMessage tone="empty" title="No series available for this section">
          The data API is reachable but serves none of these series (not synced, or not
          permitted).
        </StateMessage>
      ) : null}
      <MetricGrid layout={layout}>
        {metrics.map((metric) => (
          <MetricCard
            key={metric.code}
            metric={metric}
            series={byCode.get(metric.code)}
            calculated={calculatedFor(metric.code)}
          />
        ))}
      </MetricGrid>
    </div>
  );
}

function SeriesLoadError({ result }: { result: Exclude<ApiResult<SeriesList>, { state: "ok" }> }) {
  const unreachable = result.state === "unreachable";
  return (
    <StateMessage
      tone="error"
      title={unreachable ? "Data API unreachable" : "Data could not be loaded"}
    >
      {result.reason}. No values are shown for this section.
      {unreachable ? (
        <>
          {" "}
          Start the API: <code className="font-mono text-fg">cd backend &amp;&amp; uv run marketpulse serve</code>
        </>
      ) : null}
    </StateMessage>
  );
}
