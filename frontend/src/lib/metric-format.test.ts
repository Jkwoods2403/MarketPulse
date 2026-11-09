import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { CalculatedMetric, SeriesMetrics } from "./api.ts";
import {
  barPosition,
  direction,
  formatChange,
  formatPosition,
  formatSigned,
  headlineMetrics,
  provenanceLabel,
  shortLabel,
} from "./metric-format.ts";

const OBS = {
  period_start: "2026-10-24",
  period_end: "2026-10-30",
  source_period: "2026-10-30",
  value: "3915",
  status: "final",
};

function metric(overrides: Partial<CalculatedMetric>): CalculatedMetric {
  return {
    kind: "period_change",
    label: "Week over week",
    status: "ok",
    reason: null,
    methodology: "Latest week vs. the week ending exactly 7 days earlier.",
    current: OBS,
    comparison: { ...OBS, period_end: "2026-10-23", value: "3882" },
    absolute_change: "33",
    change_units: "Billion Cubic Feet",
    basis_points: null,
    percent_change: "0.85",
    percent_note: null,
    window: [],
    minimum: null,
    maximum: null,
    position_percent: null,
    position: null,
    ...overrides,
  };
}

describe("provenance labels", () => {
  it("labels calculated values distinctly from observed ones", () => {
    assert.equal(provenanceLabel(false), "Calculated");
    assert.notEqual(provenanceLabel(false), "Observed");
  });
  it("labels synthetic calculations as synthetic", () => {
    assert.equal(provenanceLabel(true), "Synthetic · calculated");
  });
});

describe("formatSigned / direction", () => {
  it("keeps exact decimals and signs rises", () => {
    assert.equal(formatSigned("1234.500"), "+1,234.500");
    assert.equal(formatSigned("-0.04"), "−0.04");
    assert.equal(formatSigned("0.000"), "0.000");
  });
  it("detects direction without parsing to a float", () => {
    assert.equal(direction("0.001"), "up");
    assert.equal(direction("-0.001"), "down");
    assert.equal(direction("-0.000"), "flat");
    assert.equal(direction("0"), "flat");
  });
});

describe("formatChange", () => {
  it("shows a level change with its units and percent", () => {
    assert.equal(formatChange(metric({})), "+33 Billion Cubic Feet (+0.85%)");
  });
  it("shows percentage points and basis points for rates", () => {
    const m = metric({
      absolute_change: "0.04",
      change_units: "percentage points",
      basis_points: "4.00",
      percent_change: null,
      percent_note: "Not computed: the series is already a percentage.",
    });
    assert.equal(formatChange(m), "+0.04 pp (+4.00 bp)");
  });
  it("omits an undefined percent change (zero base)", () => {
    const m = metric({ percent_change: null, percent_note: "Undefined: the comparison value is zero." });
    assert.equal(formatChange(m), "+33 Billion Cubic Feet");
  });
  it("never shows a value for missing or insufficient comparisons", () => {
    assert.equal(
      formatChange(metric({ status: "missing_comparison", absolute_change: null, percent_change: null })),
      "Not available",
    );
    assert.equal(
      formatChange(metric({ status: "insufficient_history", absolute_change: null, percent_change: null })),
      "Insufficient history",
    );
  });
});

describe("ranges", () => {
  const range = metric({
    kind: "seasonal_range",
    label: "Five-year seasonal range",
    absolute_change: null,
    percent_change: null,
    position: "within",
    position_percent: "95.2",
  });
  it("describes the position within the range", () => {
    assert.equal(formatPosition(range), "Within the range · 95.2% of min–max");
    assert.equal(barPosition(range), 95.2);
  });
  it("clamps values outside the range onto the bar", () => {
    assert.equal(barPosition({ ...range, position: "below", position_percent: "-21.2" }), 0);
    assert.equal(barPosition({ ...range, position: "above", position_percent: "130.0" }), 100);
  });
  it("has no bar position for a zero-width range", () => {
    const flat = { ...range, position_percent: null };
    assert.equal(barPosition(flat), null);
    assert.equal(formatPosition(flat), "Within the range");
  });
  it("reports insufficient history instead of a position", () => {
    const short = { ...range, status: "insufficient_history" as const, position: null, position_percent: null };
    assert.equal(formatPosition(short), "Insufficient history");
  });
});

describe("compact selection", () => {
  it("shows period change and year over year on cards", () => {
    const series = {
      metrics: [
        metric({}),
        metric({ kind: "year_over_year", label: "Year over year" }),
        metric({ kind: "seasonal_range", label: "Five-year seasonal range" }),
      ],
    } as SeriesMetrics;
    assert.deepEqual(headlineMetrics(series).map(shortLabel), ["WoW", "YoY"]);
  });
  it("labels each frequency's period change", () => {
    assert.equal(shortLabel(metric({ label: "Month over month" })), "MoM");
    assert.equal(shortLabel(metric({ label: "Quarter over quarter" })), "QoQ");
    assert.equal(shortLabel(metric({ label: "Change from previous observation" })), "Prev. obs.");
  });
});
