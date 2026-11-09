import type { CalculatedMetric, SeriesMetrics } from "./api.ts";
import { formatValue } from "./format.ts";

/**
 * Display text for calculated metrics (Phase 4). Pure, so it is unit-tested without a server.
 *
 * Calculated values are always labeled "Calculated" (or "Synthetic · calculated" for fixtures),
 * never "Observed". Values are shown with exactly the decimals the backend returned; nothing is
 * re-rounded here. Directions are shown with arrows, not gain/loss colors: a rise in
 * inventories or in a yield is not good or bad in itself.
 */

export type Direction = "up" | "down" | "flat";

export function provenanceLabel(synthetic: boolean): string {
  return synthetic ? "Synthetic · calculated" : "Calculated";
}

export function direction(value: string): Direction {
  if (/^-/.test(value) && /[1-9]/.test(value)) return "down";
  return /[1-9]/.test(value) ? "up" : "flat";
}

/** "+1,234.5", "−0.04", "0.000" (zero carries no sign). */
export function formatSigned(value: string): string {
  const dir = direction(value);
  const formatted = formatValue(value);
  return dir === "up" ? `+${formatted}` : formatted;
}

/** Short unit for a change: "pp" for percentage points, otherwise the series' own units. */
function unitLabel(units: string | null): string {
  if (units === null) return "";
  return units === "percentage points" ? "pp" : units;
}

/**
 * The headline of a change metric, e.g. "+33 Billion Cubic Feet (+0.85%)",
 * "+0.04 pp (+4.00 bp)", or the reason it is not available.
 */
export function formatChange(metric: CalculatedMetric): string {
  if (metric.status !== "ok" || metric.absolute_change === null) {
    return metric.status === "insufficient_history" ? "Insufficient history" : "Not available";
  }
  const units = unitLabel(metric.change_units);
  const parts = [`${formatSigned(metric.absolute_change)}${units ? ` ${units}` : ""}`];
  if (metric.basis_points !== null) parts.push(`(${formatSigned(metric.basis_points)} bp)`);
  else if (metric.percent_change !== null) parts.push(`(${formatSigned(metric.percent_change)}%)`);
  return parts.join(" ");
}

/** "within the range (95.2% of min–max)", "below the range", or the reason it is unknown. */
export function formatPosition(metric: CalculatedMetric): string {
  if (metric.status !== "ok" || metric.position === null) {
    return metric.status === "insufficient_history" ? "Insufficient history" : "Not available";
  }
  const where =
    metric.position === "within" ? "Within" : metric.position === "below" ? "Below" : "Above";
  return metric.position_percent === null
    ? `${where} the range`
    : `${where} the range · ${formatValue(metric.position_percent)}% of min–max`;
}

/** Position of the current value on a 0–100 bar, clamped; null if undefined. */
export function barPosition(metric: CalculatedMetric): number | null {
  if (metric.position_percent === null) return null;
  const value = Number(metric.position_percent);
  if (!Number.isFinite(value)) return null;
  return Math.min(100, Math.max(0, value));
}

/** The headline metrics for a compact card: the period change and year over year. */
export function headlineMetrics(series: SeriesMetrics): CalculatedMetric[] {
  return series.metrics.filter((m) => m.kind === "period_change" || m.kind === "year_over_year");
}

/** The range metrics for a detail panel. */
export function rangeMetrics(series: SeriesMetrics): CalculatedMetric[] {
  return series.metrics.filter((m) => m.kind === "seasonal_range" || m.kind === "trailing_extremes");
}

/** Short label for the compact card row: "WoW", "MoM", "QoQ", "1D", "YoY". */
export function shortLabel(metric: CalculatedMetric): string {
  if (metric.kind === "year_over_year") return "YoY";
  if (metric.kind === "seasonal_range") return "5Y range";
  if (metric.kind === "trailing_extremes") return "5Y low/high";
  switch (metric.label) {
    case "Week over week":
      return "WoW";
    case "Month over month":
      return "MoM";
    case "Quarter over quarter":
      return "QoQ";
    default:
      return "Prev. obs.";
  }
}
