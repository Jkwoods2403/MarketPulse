/**
 * The single statement of why the dashboard has no macroeconomic data, used wherever it says so.
 *
 * FRED is retired as a Market Pulse data provider (ADR-0015): Market Pulse obtains data only
 * directly from the original publishing organizations. No FRED series is requested or shown.
 * Macroeconomic indicators (rates, inflation, employment, output) return only when a direct
 * primary source is integrated; none is available yet. Change this text only when that changes.
 */
export const MACRO_STATUS = {
  /** Section label, where the source and cadence would otherwise be shown. */
  label: "Not available · direct sources planned",
  /** One-sentence explanation for page descriptions and section bodies. */
  summary:
    "FRED is retired as a data provider: Market Pulse uses data only directly from the " +
    "original publishing organizations. Macroeconomic indicators will return when a direct " +
    "primary source is integrated; none is available yet.",
  /** For pages that list the dashboard's coverage (the Market Overview). */
  indicators: "Macroeconomic indicators are not available yet.",
  /** Heading for the state message on the Macro page and the Overview. */
  heading: "No macroeconomic data yet",
  /** Where the decision is recorded, in the repository. */
  reference: "ADR-0015",
  /** That decision record in the public repository. */
  referenceUrl:
    "https://github.com/Jkwoods2403/MarketPulse/blob/main/docs/adr/0015-direct-source-data-only.md",
} as const;
