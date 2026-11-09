"use client";

import Link from "next/link";
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { LineChartCanvas, type LineChartControls } from "@/components/charts/line-chart-canvas";
import { RangeSelector } from "@/components/charts/range-selector";
import { FreshnessBadge, SyntheticTag } from "@/components/data/freshness-badge";
import { Skeleton, StateMessage } from "@/components/data/state-message";
import { Button } from "@/components/ui/button";
import type { ChartHistory } from "@/lib/api";
import {
  countMissing,
  summarizePoints,
  toChartPoints,
  type ChartPoint,
  type VisibleSpan,
} from "@/lib/chart-data";
import type { RangeKey } from "@/lib/chart-range";
import { cn } from "@/lib/cn";
import { formatCadence, formatPeriod, formatUtcTimestamp, formatValue } from "@/lib/format";

type Load =
  | { kind: "loading"; previous: ChartHistory | null }
  | { kind: "error"; message: string }
  | { kind: "ready"; data: ChartHistory };

/**
 * Historical chart for one series, loaded through the app's own route handler
 * (/api/series/{code}/observations), which proxies the read-only backend API. Restricted and
 * blocked series are refused by the backend and arrive here as "not available".
 *
 * Every chart shows its title, units, cadence, freshness, the exact range shown, the number of
 * observations and of periods missing at source, its source attribution, when Market Pulse last
 * retrieved the data, and the TradingView attribution. Synthetic fixtures are labeled. When the
 * user zooms or pans, the caption names the observations actually in view and a Reset view
 * button returns to the whole selected range.
 *
 * The canvas is not readable by assistive technology, so the figure is described by a text
 * summary (range, latest, lowest and highest values with their periods, gaps), which is
 * announced again when the range changes. Nothing is conveyed by color alone: synthetic lines
 * are also dashed, watermarked, and tagged; gaps are counted in text.
 */
export function TimeSeriesChart({
  code,
  title,
  note,
  height = 220,
}: {
  code: string;
  title: string;
  note?: string;
  height?: number;
}) {
  const id = useId();
  const headingId = `${id}-title`;
  const summaryId = `${id}-summary`;
  const [range, setRange] = useState<RangeKey | null>(null);
  const [load, setLoad] = useState<Load>({ kind: "loading", previous: null });
  const [span, setSpan] = useState<VisibleSpan | null>(null);
  const figureRef = useRef<HTMLElement>(null);
  const controlsRef = useRef<LineChartControls>(null);

  // The button disappears once the view is reset, so keep keyboard focus on the chart.
  const resetView = () => {
    controlsRef.current?.resetView();
    figureRef.current?.focus();
  };

  // Changing the range marks the chart as loading (keeping the previous data on screen); the
  // effect below then fetches the new range.
  const selectRange = (key: RangeKey) => {
    setLoad((prev) => ({
      kind: "loading",
      previous: prev.kind === "ready" ? prev.data : prev.kind === "loading" ? prev.previous : null,
    }));
    setRange(key);
  };

  useEffect(() => {
    const controller = new AbortController();
    const query = range ? `?range=${range}` : "";
    fetch(`/api/series/${encodeURIComponent(code)}/observations${query}`, {
      signal: controller.signal,
      cache: "no-store",
    })
      .then(async (response) => {
        const body: unknown = await response.json().catch(() => null);
        if (!response.ok) {
          const message =
            typeof body === "object" && body !== null && "error" in body
              ? String((body as { error: unknown }).error)
              : `HTTP ${response.status}`;
          setLoad({ kind: "error", message });
          return;
        }
        setLoad({ kind: "ready", data: body as ChartHistory });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLoad({
          kind: "error",
          message: error instanceof Error ? error.message : "Request failed",
        });
      });
    return () => controller.abort();
  }, [code, range]);

  const data = load.kind === "ready" ? load.data : load.kind === "loading" ? load.previous : null;
  const points = useMemo(
    () => (data ? toChartPoints(data.series.period_convention, data.observations) : []),
    [data],
  );

  return (
    <figure
      ref={figureRef}
      tabIndex={-1}
      aria-labelledby={headingId}
      aria-describedby={summaryId}
      aria-busy={load.kind === "loading"}
      className={cn(
        "flex min-w-0 flex-col border bg-panel",
        data?.series.synthetic ? "border-dashed border-status-delayed/50" : "border-border",
      )}
    >
      <figcaption className="space-y-1.5 border-b border-border px-3 py-2">
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0">
            <h3 id={headingId} className="truncate text-xs font-medium text-fg">{title}</h3>
            <p className="truncate text-[11px] text-muted">
              {data
                ? `${data.series.units} · ${formatCadence(data.series.frequency, data.series.period_convention)}`
                : " "}
            </p>
          </div>
          <div className="flex shrink-0 flex-wrap justify-end gap-1">
            {data?.series.synthetic ? <SyntheticTag /> : null}
            {data ? <FreshnessBadge status={data.series.freshness.status} /> : null}
          </div>
        </div>
        {note ? <p className="text-[11px] leading-4 text-status-delayed">{note}</p> : null}
        {data ? (
          <div className="flex flex-wrap items-center gap-2">
            <RangeSelector
              options={data.options}
              active={data.range}
              busy={load.kind === "loading"}
              onSelect={selectRange}
            />
            {span?.zoomed ? (
              <Button variant="segment" onClick={resetView}>
                Reset view
              </Button>
            ) : null}
          </div>
        ) : null}
      </figcaption>

      <div className="relative">
        {load.kind === "error" ? (
          <div className="p-3" style={{ minHeight: height }}>
            <StateMessage tone="error" title="History could not be loaded">
              {load.message}. No values are shown.
            </StateMessage>
          </div>
        ) : data === null ? (
          <div className="p-3" style={{ height }} aria-busy="true">
            <Skeleton className="h-full w-full" />
          </div>
        ) : points.length === 0 ? (
          <div className="p-3" style={{ minHeight: height }}>
            <StateMessage tone="empty" title="No observations stored">
              This series has no stored history yet. Nothing is drawn rather than an empty line.
            </StateMessage>
          </div>
        ) : (
          <LineChartCanvas
            points={points}
            convention={data.series.period_convention}
            units={data.series.units}
            synthetic={data.series.synthetic}
            height={height}
            onVisibleSpanChange={setSpan}
            controls={controlsRef}
          />
        )}
        {load.kind === "loading" && data !== null ? (
          <div className="absolute right-2 bottom-2 font-mono text-[10px] text-faint">loading…</div>
        ) : null}
      </div>

      <p id={summaryId} aria-live="polite" className="sr-only">
        {load.kind === "error"
          ? `History could not be loaded: ${load.message}.`
          : data === null
            ? "Loading history."
            : describe(title, data, points)}
      </p>

      {data ? <ChartFooter data={data} points={points} span={span} /> : null}
    </figure>
  );
}

/** Text alternative for the chart canvas. Values are the exact published strings. */
function describe(title: string, data: ChartHistory, points: readonly ChartPoint[]): string {
  const convention = data.series.period_convention;
  const units = data.series.units;
  const summary = summarizePoints(points);
  const period = (o: { period_start: string; period_end: string }) =>
    formatPeriod(convention, o.period_start, o.period_end);
  const value = (o: { value: string | null }) => `${o.value === null ? "—" : formatValue(o.value)} ${units}`;
  if (summary.first === null || summary.last === null) {
    return `${title}: no observations stored; nothing is drawn.`;
  }
  const parts = [
    `${data.series.synthetic ? "Synthetic development data, not market data. " : ""}Line chart of ${title}, ${data.range} range: ${summary.count} observations from ${period(summary.first)} to ${period(summary.last)}.`,
  ];
  if (summary.latest) parts.push(`Latest ${value(summary.latest)} (${period(summary.latest)}).`);
  if (summary.low && summary.high) {
    parts.push(
      `Lowest ${value(summary.low)} (${period(summary.low)}); highest ${value(summary.high)} (${period(summary.high)}).`,
    );
  }
  if (summary.missing > 0) parts.push(`${summary.missing} periods missing at source, shown as gaps.`);
  return parts.join(" ");
}

function ChartFooter({
  data,
  points,
  span,
}: {
  data: ChartHistory;
  points: ReturnType<typeof toChartPoints>;
  span: VisibleSpan | null;
}) {
  const convention = data.series.period_convention;
  const first = points[0]?.raw;
  const last = points.at(-1)?.raw;
  const missing = countMissing(points);
  const producer =
    data.series.original_source && data.series.original_source !== data.series.source.name
      ? ` Original producer: ${data.series.original_source}.`
      : "";
  return (
    <footer className="space-y-0.5 border-t border-border px-3 py-1.5 text-[10px] leading-4 text-faint">
      {first && last ? (
        <p>
          {span?.zoomed ? (
            <>
              Viewing {formatPeriod(convention, span.first.period_start, span.first.period_end)} –{" "}
              {formatPeriod(convention, span.last.period_start, span.last.period_end)} · {span.count}{" "}
              of {span.total} observations
            </>
          ) : (
            <>
              Showing {formatPeriod(convention, first.period_start, first.period_end)} –{" "}
              {formatPeriod(convention, last.period_start, last.period_end)} · {points.length}{" "}
              observations
            </>
          )}
          {missing > 0 ? ` · ${missing} missing at source (gaps)` : ""}
          {data.truncated ? ` · limited to the first ${points.length} of ${data.total}` : ""}
          {data.earliest ? ` · stored since ${data.earliest}` : ""}
        </p>
      ) : null}
      <p>
        {data.series.source.attribution ?? `Source: ${data.series.source.name}`}.{producer}
        {data.series.latest
          ? ` Latest value last confirmed by Market Pulse ${formatUtcTimestamp(data.series.latest.last_confirmed_at)}.`
          : ""}
      </p>
      {/* The attribution notice from the Lightweight Charts NOTICE file (v5.2.1), verbatim,
          with the required link to tradingview.com; full texts on /notices. */}
      <p>
        {/* "(с)" is the Cyrillic letter the NOTICE file uses, kept as published. */}
        {"TradingView Lightweight Charts™ Copyright (с) 2025 TradingView, Inc. "}
        <a
          href="https://www.tradingview.com/"
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-dotted hover:text-muted"
        >
          https://www.tradingview.com/
        </a>{" "}
        · Licensed under the{" "}
        <a
          href="https://www.apache.org/licenses/LICENSE-2.0"
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-dotted hover:text-muted"
        >
          Apache License 2.0
        </a>{" "}
        ·{" "}
        <Link href="/notices" className="underline decoration-dotted hover:text-muted">
          Third-party notices
        </Link>
      </p>
    </footer>
  );
}
