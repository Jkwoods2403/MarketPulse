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
 */

import { FRED_LICENSING_STATUS } from "./fred-status.ts";

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

  // Rates and dollar (FRED)
  ust3m: { code: "ust_3m", label: "3-month Treasury yield" },
  ust2y: { code: "ust_2y", label: "2-year Treasury yield" },
  ust10y: { code: "ust_10y", label: "10-year Treasury yield" },
  ust30y: { code: "ust_30y", label: "30-year Treasury yield" },
  curve10y2y: {
    code: "ust_10y_2y_spread",
    label: "10-year minus 2-year spread",
    note: `FRED's published series, not a Market Pulse calculation. ${FRED_LICENSING_STATUS.note}`,
  },
  fedFunds: {
    code: "fed_funds_effective",
    label: "Effective federal funds rate",
    note: "Calendar-day series: includes weekends and holidays.",
  },
  usdIndex: {
    code: "usd_broad_index",
    label: "Nominal broad U.S. dollar index",
    note: "Published with a reporting lag; check the observation date.",
  },

  // Economy (FRED)
  cpi: { code: "cpi_all_items", label: "Consumer Price Index, all items" },
  payrolls: { code: "nonfarm_payrolls", label: "Nonfarm payrolls" },
  unemployment: { code: "unemployment_rate", label: "Unemployment rate" },
  industrialProduction: { code: "industrial_production", label: "Industrial production" },
  realGdp: { code: "real_gdp", label: "Real GDP" },
} satisfies Record<string, MetricDefinition>;

export const KEY_INDICATORS: readonly MetricDefinition[] = [
  METRICS.crudeStocks,
  METRICS.gasStorage,
  METRICS.refineryUtilization,
  METRICS.ust10y,
  METRICS.curve10y2y,
  METRICS.fedFunds,
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

export const RATES: readonly MetricDefinition[] = [
  METRICS.ust3m,
  METRICS.ust2y,
  METRICS.ust10y,
  METRICS.ust30y,
  METRICS.curve10y2y,
  METRICS.fedFunds,
  METRICS.usdIndex,
];

export const ECONOMY: readonly MetricDefinition[] = [
  METRICS.cpi,
  METRICS.payrolls,
  METRICS.unemployment,
  METRICS.industrialProduction,
  METRICS.realGdp,
];

/** Every FRED series on the dashboard. All are unavailable (see fred-status.ts). */
export const FRED_SERIES_CODES: ReadonlySet<string> = new Set(
  [...RATES, ...ECONOMY].map((metric) => metric.code),
);

/** `metrics` without its FRED series: no FRED values are shown, so none are attributed. */
export function withoutFred(metrics: readonly MetricDefinition[]): readonly MetricDefinition[] {
  return metrics.filter((metric) => !FRED_SERIES_CODES.has(metric.code));
}

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
