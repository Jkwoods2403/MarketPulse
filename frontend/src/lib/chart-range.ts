/**
 * Date ranges for historical charts. Pure functions with no imports, so they run unchanged in
 * the browser, in route handlers, and under `node --test`.
 *
 * Ranges are anchored on the series' latest observation, not on today, and are constrained by
 * the history that actually exists: a range is offered only if the stored history reaches back
 * that far and the range can hold at least two periods of the series' frequency. "Max" always
 * shows everything stored.
 */

export type RangeKey = "1M" | "3M" | "6M" | "1Y" | "5Y" | "MAX";

export const RANGES: ReadonlyArray<{ key: RangeKey; label: string; months: number | null }> = [
  { key: "1M", label: "1M", months: 1 },
  { key: "3M", label: "3M", months: 3 },
  { key: "6M", label: "6M", months: 6 },
  { key: "1Y", label: "1Y", months: 12 },
  { key: "5Y", label: "5Y", months: 60 },
  { key: "MAX", label: "Max", months: null },
];

export function isRangeKey(value: string): value is RangeKey {
  return RANGES.some((r) => r.key === value);
}

// Length of one period in months (exact for monthly and longer), used only to decide whether a
// range is too short: a range must span at least two periods.
const DAYS_PER_MONTH = 30.44;
const PERIOD_MONTHS: Record<string, number> = {
  fifteen_minute: 1 / (96 * DAYS_PER_MONTH),
  hourly: 1 / (24 * DAYS_PER_MONTH),
  daily: 1 / DAYS_PER_MONTH,
  weekly: 7 / DAYS_PER_MONTH,
  monthly: 1,
  quarterly: 3,
  annual: 12,
};

function parseDate(iso: string): Date {
  const [y, m, d] = iso.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d));
}

function isoDate(date: Date): string {
  return date.toISOString().slice(0, 10);
}

/** `latest` minus the range's months (day clamped to the target month's length); null for Max. */
export function rangeStart(key: RangeKey, latest: string): string | null {
  const months = RANGES.find((r) => r.key === key)?.months ?? null;
  if (months === null) return null;
  const end = parseDate(latest);
  const year = end.getUTCFullYear();
  const month = end.getUTCMonth() - months;
  const lastDay = new Date(Date.UTC(year, month + 1, 0)).getUTCDate();
  return isoDate(new Date(Date.UTC(year, month, Math.min(end.getUTCDate(), lastDay))));
}

export type RangeOption = { key: RangeKey; label: string; enabled: boolean; reason?: string };

/**
 * Which ranges a series can show. `earliest` and `latest` are the period_start dates of the
 * oldest and newest stored observations (null when nothing is stored).
 */
export function rangeOptions(
  frequency: string,
  earliest: string | null,
  latest: string | null,
): RangeOption[] {
  return RANGES.map(({ key, label, months }) => {
    if (earliest === null || latest === null) {
      return { key, label, enabled: false, reason: "No observations stored" };
    }
    if (months === null) return { key, label, enabled: true };
    const periodMonths = PERIOD_MONTHS[frequency] ?? PERIOD_MONTHS.daily;
    if (months < 2 * periodMonths) {
      return { key, label, enabled: false, reason: `Too short for ${frequency} data` };
    }
    const start = rangeStart(key, latest);
    if (start !== null && start < earliest) {
      return {
        key,
        label,
        enabled: false,
        reason: `Stored history starts ${earliest}; use Max`,
      };
    }
    return { key, label, enabled: true };
  });
}

const PREFERRED: Record<string, RangeKey[]> = {
  daily: ["1Y", "6M", "3M", "MAX"],
  weekly: ["5Y", "1Y", "MAX"],
  monthly: ["5Y", "MAX"],
  quarterly: ["MAX"],
};

/** The first enabled range from the frequency's preferences, else Max. */
export function defaultRange(frequency: string, options: readonly RangeOption[]): RangeKey {
  const enabled = new Set(options.filter((o) => o.enabled).map((o) => o.key));
  return (PREFERRED[frequency] ?? ["1Y", "MAX"]).find((k) => enabled.has(k)) ?? "MAX";
}
