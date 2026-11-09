/**
 * Display formatting that preserves source semantics. Dates are formatted in UTC from their
 * ISO strings, so a period date never shifts by a day in another time zone. Values keep exactly
 * the decimals the source published; only thousands separators are added.
 */

const DAY = new Intl.DateTimeFormat("en-US", {
  timeZone: "UTC",
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
});
const MONTH = new Intl.DateTimeFormat("en-US", { timeZone: "UTC", month: "short", year: "numeric" });

function utcDate(isoDate: string): Date {
  return new Date(`${isoDate}T00:00:00Z`);
}

/** "1234567.890" -> "1,234,567.890" (no rounding, no added or removed decimals). */
export function formatValue(value: string): string {
  const negative = value.startsWith("-");
  const [whole, fraction] = (negative ? value.slice(1) : value).split(".");
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "−" : ""}${grouped}${fraction !== undefined ? `.${fraction}` : ""}`;
}

/** The period an observation describes, in the source's own terms. */
export function formatPeriod(convention: string, periodStart: string, periodEnd: string): string {
  if (convention.startsWith("week_ending_")) return `Week ending ${DAY.format(utcDate(periodEnd))}`;
  if (convention === "month") return MONTH.format(utcDate(periodStart));
  if (convention === "quarter") {
    const start = utcDate(periodStart);
    return `${start.getUTCFullYear()} Q${Math.floor(start.getUTCMonth() / 3) + 1}`;
  }
  if (convention === "business_day" || convention === "calendar_day") {
    return DAY.format(utcDate(periodEnd));
  }
  return periodStart === periodEnd ? periodStart : `${periodStart} – ${periodEnd}`;
}

const CONVENTION_LABELS: Record<string, string> = {
  business_day: "business days",
  calendar_day: "calendar days",
  month: "month",
  quarter: "quarter",
};

/** "Weekly · week ending Friday", "Daily · business days", "Monthly · month". */
export function formatCadence(frequency: string, convention: string): string {
  const freq = frequency.charAt(0).toUpperCase() + frequency.slice(1).replace("_", " ");
  const conv = convention.startsWith("week_ending_")
    ? `week ending ${convention.slice("week_ending_".length).replace(/^./, (c) => c.toUpperCase())}`
    : (CONVENTION_LABELS[convention] ?? convention.replaceAll("_", " "));
  return `${freq} · ${conv}`;
}

/** "2026-11-06 12:00 UTC" */
export function formatUtcTimestamp(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}
