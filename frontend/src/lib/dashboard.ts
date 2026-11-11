/**
 * Which series the dashboard shows, where, and under what short label. Units, frequency,
 * period conventions, source attribution, values, and freshness all come from the data API
 * (GET /series); nothing about a series' data is defined here.
 *
 * `note` states a semantic that is easy to misread (a monthly total vs. a daily rate, a
 * reporting lag); it never contains a value.
 *
 * The API serves only permitted series. A code listed here that the API does not return is
 * shown as unavailable, never filled in.
 *
 * Only series from active, approved sources are listed (currently EIA). FRED is retired as a
 * data provider (ADR-0015), so no FRED series is defined here; macroeconomic indicators return
 * when a direct primary source is integrated.
 */

export type MetricDefinition = {
  code: string; // catalog series code
  label: string;
  note?: string;
};

export const METRICS = {
  // Crude oil and petroleum (EIA, weekly, week ending Friday)
  crudeStocks: { code: "us_crude_stocks_ex_spr", label: "U.S. crude stocks excl. SPR" },
  sprStocks: { code: "us_crude_stocks_spr", label: "Strategic Petroleum Reserve" },
  gasolineStocks: { code: "us_gasoline_stocks", label: "Total gasoline stocks" },
  distillateStocks: { code: "us_distillate_stocks", label: "Distillate fuel oil stocks" },
  crudeProduction: {
    code: "us_crude_production",
    label: "U.S. crude oil field production",
    note: "A daily rate, not a weekly total.",
  },
  refineryUtilization: {
    code: "us_refinery_utilization",
    label: "Refinery utilization of operable capacity",
  },

  // Natural gas (EIA)
  gasStorage: { code: "l48_gas_storage", label: "Lower 48 working gas in storage" },
  dryGasProduction: {
    code: "us_dry_gas_production",
    label: "U.S. dry natural gas production",
    note: "Monthly total, not a daily rate.",
  },
  lngExports: {
    code: "us_lng_exports",
    label: "U.S. LNG exports",
    note: "Monthly total, not a daily rate.",
  },
} satisfies Record<string, MetricDefinition>;

export const KEY_INDICATORS: readonly MetricDefinition[] = [
  METRICS.crudeStocks,
  METRICS.gasStorage,
  METRICS.refineryUtilization,
];

export const CRUDE_PETROLEUM: readonly MetricDefinition[] = [
  METRICS.crudeStocks,
  METRICS.sprStocks,
  METRICS.gasolineStocks,
  METRICS.distillateStocks,
  METRICS.crudeProduction,
  METRICS.refineryUtilization,
];

export const NATURAL_GAS: readonly MetricDefinition[] = [
  METRICS.gasStorage,
  METRICS.dryGasProduction,
  METRICS.lngExports,
];

export type UnavailablePrice = {
  label: string;
  benchmark: string;
  units: string;
};

/**
 * Spot prices that exist in the catalog but may not be used (personal use only). The API never
 * serves them; this static panel explains the gap.
 */
export const UNAVAILABLE_PRICES: readonly UnavailablePrice[] = [
  { label: "WTI crude oil", benchmark: "Cushing, OK spot", units: "$/barrel" },
  { label: "Brent crude oil", benchmark: "Europe spot", units: "$/barrel" },
  { label: "Henry Hub natural gas", benchmark: "Louisiana spot", units: "$/MMBtu" },
];

export const PRICE_UNAVAILABLE_REASON =
  "Spot prices published by EIA are Refinitiv (LSEG) data licensed for personal use only. " +
  "Market Pulse does not ingest or display them until a licensed price source is selected.";
