"use client";

import {
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  createTextWatermark,
  type IChartApi,
  type LogicalRange,
  type MouseEventParams,
  type Time,
} from "lightweight-charts";
import { useEffect, useImperativeHandle, useRef, useState, type Ref } from "react";

import {
  legendTopMargin,
  visibleSpan,
  type ChartPoint,
  type VisibleSpan,
} from "@/lib/chart-data";
import { formatPeriod, formatValue } from "@/lib/format";

type Hover = { point: ChartPoint; x: number; width: number } | null;

export type LineChartControls = { resetView: () => void };

// The library's default (0.5 px) cannot fit a long history into a narrow panel: fitContent would
// silently leave the oldest observations out of view. This fits tens of thousands of points.
const MIN_BAR_SPACING_PX = 0.005;

function token(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(`--color-${name}`).trim();
}

/** Lightweight Charts' time back to our "YYYY-MM-DD" key (string or BusinessDay input). */
function timeToKey(time: Time | undefined): string | null {
  if (time === undefined) return null;
  if (typeof time === "string") return time;
  if (typeof time === "number") return new Date(time * 1000).toISOString().slice(0, 10);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${time.year}-${pad(time.month)}-${pad(time.day)}`;
}

function decimalsIn(points: readonly ChartPoint[]): number {
  let max = 0;
  for (const p of points) {
    const fraction = p.raw.value?.split(".")[1];
    if (fraction) max = Math.max(max, fraction.length);
  }
  return Math.min(max, 3);
}

/**
 * One series as a line. Periods the source reported as missing are gaps (whitespace points);
 * nothing is interpolated. The tooltip shows the exact published value string, while axis
 * labels may round. TradingView's attribution logo (a link to tradingview.com) is enabled, as
 * the Lightweight Charts license requires.
 *
 * The legend gives the latest value, so the price axis has no last-value tag, and the price
 * scale keeps a band free at the top so the line never runs under the legend. The observations
 * in view are reported through `onVisibleSpanChange` as the user zooms or pans.
 */
export function LineChartCanvas({
  points,
  convention,
  units,
  synthetic,
  height,
  onVisibleSpanChange,
  controls,
}: {
  points: readonly ChartPoint[];
  convention: string;
  units: string;
  synthetic: boolean;
  height: number;
  onVisibleSpanChange?: (span: VisibleSpan | null) => void;
  controls?: Ref<LineChartControls>;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const onSpanRef = useRef(onVisibleSpanChange);
  const [hover, setHover] = useState<Hover>(null);

  useEffect(() => {
    onSpanRef.current = onVisibleSpanChange;
  }, [onVisibleSpanChange]);

  useImperativeHandle(
    controls,
    () => ({ resetView: () => chartRef.current?.timeScale().fitContent() }),
    [],
  );

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const decimals = decimalsIn(points);
    const axisFormat = new Intl.NumberFormat("en-US", {
      minimumFractionDigits: 0,
      maximumFractionDigits: decimals,
    });
    const chart = createChart(container, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: token("panel") },
        textColor: token("muted"),
        fontFamily: getComputedStyle(document.body).fontFamily,
        fontSize: 11,
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: token("border") },
        horzLines: { color: token("border") },
      },
      rightPriceScale: {
        borderColor: token("border-strong"),
        scaleMargins: { top: legendTopMargin(height), bottom: 0.05 },
      },
      timeScale: {
        borderColor: token("border-strong"),
        timeVisible: false,
        minBarSpacing: MIN_BAR_SPACING_PX,
        // Keep the view on the data: without these, zooming out shrinks the line to a sliver.
        fixLeftEdge: true,
        fixRightEdge: true,
      },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: token("faint"), labelBackgroundColor: token("panel-raised") },
        horzLine: { color: token("faint"), labelBackgroundColor: token("panel-raised") },
      },
      localization: { priceFormatter: (price: number) => axisFormat.format(price) },
    });

    const series = chart.addSeries(LineSeries, {
      color: synthetic ? token("status-delayed") : token("accent"),
      lineWidth: 2,
      lineStyle: synthetic ? LineStyle.Dashed : LineStyle.Solid,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerRadius: 3,
    });
    series.setData(
      points.map((p) => (p.value === undefined ? { time: p.time } : { time: p.time, value: p.value })),
    );

    if (synthetic) {
      createTextWatermark(chart.panes()[0], {
        horzAlign: "center",
        vertAlign: "center",
        lines: [{ text: "SYNTHETIC · NOT MARKET DATA", color: "rgba(224, 163, 58, 0.22)", fontSize: 18 }],
      });
    }
    chartRef.current = chart;

    let reported = "";
    const onVisibleRange = (range: LogicalRange | null) => {
      const span = range ? visibleSpan(points, range.from, range.to) : null;
      const key = span ? `${span.firstIndex}:${span.lastIndex}` : "";
      if (key === reported) return; // panning fires per frame; report only real changes
      reported = key;
      onSpanRef.current?.(span);
    };
    chart.timeScale().subscribeVisibleLogicalRangeChange(onVisibleRange);
    chart.timeScale().fitContent();

    const byTime = new Map(points.map((p) => [p.time, p]));
    const onMove = (param: MouseEventParams<Time>) => {
      const key = timeToKey(param.time);
      const point = key ? byTime.get(key) : undefined;
      setHover(
        point && param.point
          ? { point, x: param.point.x, width: container.clientWidth }
          : null,
      );
    };
    chart.subscribeCrosshairMove(onMove);

    return () => {
      chart.unsubscribeCrosshairMove(onMove);
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onVisibleRange);
      chartRef.current = null;
      onSpanRef.current?.(null);
      chart.remove();
    };
  }, [points, synthetic, height]);

  const shown = hover?.point ?? points.at(-1) ?? null;
  return (
    <div className="relative" style={{ height }}>
      <div ref={containerRef} className="absolute inset-0" />
      {shown ? (
        <div
          aria-hidden
          className="pointer-events-none absolute top-1.5 z-10 max-w-[70%] border border-border-strong bg-panel-raised/95 px-2 py-1 font-mono text-[11px] leading-4 shadow"
          style={
            hover === null
              ? { left: 8 }
              : hover.x > hover.width / 2
                ? { right: Math.max(hover.width - hover.x + 12, 4) }
                : { left: hover.x + 12 }
          }
        >
          <div className="text-muted">
            {formatPeriod(convention, shown.raw.period_start, shown.raw.period_end)}
            {hover ? null : " · latest"}
          </div>
          {shown.raw.value === null ? (
            <div className="text-status-unavailable">Missing at source</div>
          ) : (
            <div className="text-fg">
              {formatValue(shown.raw.value)} <span className="text-faint">{units}</span>
              {synthetic ? <span className="ml-1 text-status-delayed">SYNTHETIC</span> : null}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
