/**
 * The single statement of FRED's licensing status, used wherever the dashboard names FRED.
 *
 * FRED's terms (legal clause (p), API clause (l)) require the Federal Reserve Bank of St. Louis's
 * prior written consent to store FRED content in a database. That consent has not been received,
 * so Market Pulse does not ingest new FRED data and shows no FRED values. The full record is in
 * docs/data-sources.md (FRED section). Change this text only when that status changes.
 */
export const FRED_LICENSING_STATUS = {
  /** Section label, where the source and cadence would otherwise be shown. */
  label: "FRED · unavailable (licensing)",
  /** One-sentence explanation for page descriptions. */
  summary:
    "FRED data is unavailable: storing it requires the prior written consent of the Federal " +
    "Reserve Bank of St. Louis, which has not been received.",
  /** For pages that list the dashboard's coverage (the Market Overview). */
  indicators: "Macroeconomic indicators from FRED are unavailable pending licensing consent.",
  /** Short form for per-series notes. */
  note: "Unavailable pending FRED licensing consent.",
  /** Heading for the Macro page's calculated-metrics area. */
  metricsHeading: "Calculated metrics unavailable",
  /** Why no metrics are calculated for FRED series. */
  metrics:
    "Market Pulse derives calculated metrics only from stored observations. FRED data is not " +
    "stored, pending licensing consent, so no changes, ranges, or other metrics are calculated " +
    "for these series.",
  /** Where the licensing decision is documented, in the repository. */
  reference: "docs/data-sources.md",
  /** The FRED section of that document in the public repository. */
  referenceUrl:
    "https://github.com/Jkwoods2403/MarketPulse/blob/main/docs/data-sources.md" +
    "#fred-federal-reserve-bank-of-st-louis",
} as const;
