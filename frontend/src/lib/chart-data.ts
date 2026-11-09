/**
 * Turns API observations into chart points. Pure, with no imports (see chart-range.ts).
 *
 * - Each point's time is the date the period is labeled by: the week's end for "week ending"
 *   series, otherwise the period's start (the month, quarter, or day itself).
 * - A period the source reported as missing becomes a gap (a point with no value). Nothing is
 *   interpolated, carried forward, or filled in, and periods the source never reported are not
 *   invented: the chart shows only what the API returned.
 */

export type ChartObservation = {
  period_start: string;
  period_end: string;
  source_period: string | null;
  value: string | null;
  status: string;
};

export type ChartPoint =
  | { time: string; value: number; raw: ChartObservation }
  | { time: string; value: undefined; raw: ChartObservation };

export function timeKey(convention: string, observation: ChartObservation): string {
  return convention.startsWith("week_ending_") ? observation.period_end : observation.period_start;
}

export function toChartPoints(
  convention: string,
  observations: readonly ChartObservation[],
): ChartPoint[] {
  const seen = new Set<string>();
  const points: ChartPoint[] = [];
  for (const observation of observations) {
    const time = timeKey(convention, observation);
    if (seen.has(time)) continue; // the API never returns two current versions of a period
    seen.add(time);
    const value = observation.value === null ? Number.NaN : Number(observation.value);
    points.push(
      Number.isFinite(value)
        ? { time, value, raw: observation }
        : { time, value: undefined, raw: observation },
    );
  }
  return points.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
}

export function countMissing(points: readonly ChartPoint[]): number {
  return points.filter((p) => p.value === undefined).length;
}

export type VisibleSpan = {
  firstIndex: number;
  lastIndex: number;
  first: ChartObservation;
  last: ChartObservation;
  count: number;
  total: number;
  zoomed: boolean; // part of the loaded data is out of view
};

/**
 * The observations in view, from a chart's logical range (fractional point indexes, which may
 * extend past either end of the data). Null when nothing is in view.
 */
export function visibleSpan(
  points: readonly ChartPoint[],
  from: number,
  to: number,
): VisibleSpan | null {
  const total = points.length;
  const firstIndex = Math.max(0, Math.ceil(from));
  const lastIndex = Math.min(total - 1, Math.floor(to));
  if (total === 0 || firstIndex > lastIndex) return null;
  return {
    firstIndex,
    lastIndex,
    first: points[firstIndex].raw,
    last: points[lastIndex].raw,
    count: lastIndex - firstIndex + 1,
    total,
    zoomed: firstIndex > 0 || lastIndex < total - 1,
  };
}

const LEGEND_BAND_PX = 60; // the legend's height plus its offset from the top of the chart
const TIME_AXIS_PX = 28;
const MAX_TOP_MARGIN = 0.45;

/** Share of the price scale kept empty at the top, so the line never runs under the legend. */
export function legendTopMargin(chartHeight: number): number {
  return Math.min(MAX_TOP_MARGIN, LEGEND_BAND_PX / (chartHeight - TIME_AXIS_PX));
}

export type ChartSummary = {
  count: number;
  missing: number;
  first: ChartObservation | null;
  last: ChartObservation | null;
  latest: ChartObservation | null; // newest observation with a value
  low: ChartObservation | null; // earliest observation with the lowest value
  high: ChartObservation | null; // earliest observation with the highest value
};

/**
 * What a chart shows, as data for its text alternative: the range covered, the latest, lowest,
 * and highest values (as the exact published observations), and the gaps.
 */
export function summarizePoints(points: readonly ChartPoint[]): ChartSummary {
  let low: ChartPoint | null = null;
  let high: ChartPoint | null = null;
  let latest: ChartPoint | null = null;
  for (const p of points) {
    if (p.value === undefined) continue;
    if (low === null || p.value < (low.value as number)) low = p;
    if (high === null || p.value > (high.value as number)) high = p;
    latest = p;
  }
  return {
    count: points.length,
    missing: countMissing(points),
    first: points[0]?.raw ?? null,
    last: points.at(-1)?.raw ?? null,
    latest: latest?.raw ?? null,
    low: low?.raw ?? null,
    high: high?.raw ?? null,
  };
}
