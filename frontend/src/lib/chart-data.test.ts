// Run with `pnpm test`.
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  countMissing,
  legendTopMargin,
  summarizePoints,
  toChartPoints,
  visibleSpan,
  type ChartObservation,
} from "./chart-data.ts";

const obs = (start: string, end: string, value: string | null): ChartObservation => ({
  period_start: start,
  period_end: end,
  source_period: end,
  value,
  status: value === null ? "missing_at_source" : "ok",
});

describe("visibleSpan", () => {
  const points = toChartPoints(
    "month",
    ["01", "02", "03", "04", "05"].map((m) => obs(`2026-${m}-01`, `2026-${m}-28`, m)),
  );

  it("is not zoomed when every point is in view, even past the data's ends", () => {
    const span = visibleSpan(points, -0.5, 4.5);
    assert.equal(span?.count, 5);
    assert.equal(span?.zoomed, false);
    assert.equal(visibleSpan(points, -30, 40)?.zoomed, false);
  });

  it("is not zoomed when a fitted view misses the ends by floating-point error", () => {
    assert.equal(visibleSpan(points, 1e-9, 4 - 1e-9)?.zoomed, false);
    assert.equal(visibleSpan(points, 1e-9, 4 - 1e-9)?.count, 5);
  });

  it("reports only the whole points in view when zoomed", () => {
    const span = visibleSpan(points, 0.6, 3.4);
    assert.equal(span?.zoomed, true);
    assert.equal(span?.count, 3);
    assert.equal(span?.total, 5);
    assert.equal(span?.first.period_start, "2026-02-01");
    assert.equal(span?.last.period_start, "2026-04-01");
  });

  it("treats a view that excludes only the newest point as zoomed", () => {
    assert.equal(visibleSpan(points, 0, 3)?.zoomed, true);
  });

  it("returns null when nothing is in view or there is no data", () => {
    assert.equal(visibleSpan(points, 1.2, 1.8), null);
    assert.equal(visibleSpan(points, 9, 12), null);
    assert.equal(visibleSpan([], 0, 4), null);
  });
});

describe("legendTopMargin", () => {
  it("reserves the legend's height on the chart sizes in use", () => {
    assert.ok(Math.abs(legendTopMargin(220) - 60 / 192) < 1e-9);
    assert.ok(Math.abs(legendTopMargin(180) - 60 / 152) < 1e-9);
  });

  it("never gives up more than the maximum share of a short chart", () => {
    assert.equal(legendTopMargin(100), 0.45);
  });
});

describe("toChartPoints", () => {
  it("places weekly observations on the week-ending date", () => {
    const points = toChartPoints("week_ending_friday", [obs("2026-10-24", "2026-10-30", "430.250")]);
    assert.equal(points[0].time, "2026-10-30");
  });

  it("places monthly and quarterly observations on the period start", () => {
    assert.equal(toChartPoints("month", [obs("2026-08-01", "2026-08-31", "1")])[0].time, "2026-08-01");
    assert.equal(
      toChartPoints("quarter", [obs("2026-07-01", "2026-09-30", "1")])[0].time,
      "2026-07-01",
    );
  });

  it("turns missing-at-source periods into gaps, never into values", () => {
    const points = toChartPoints("business_day", [
      obs("2026-10-27", "2026-10-27", "4.180"),
      obs("2026-10-28", "2026-10-28", null),
      obs("2026-10-29", "2026-10-29", "4.190"),
    ]);
    assert.deepEqual(
      points.map((p) => p.value),
      [4.18, undefined, 4.19],
    );
    assert.equal(countMissing(points), 1);
  });

  it("does not invent periods the source never reported", () => {
    // No observation for 2026-10-17: no point is created for it.
    const points = toChartPoints("week_ending_friday", [
      obs("2026-10-03", "2026-10-09", "1"),
      obs("2026-10-17", "2026-10-23", "2"),
    ]);
    assert.deepEqual(
      points.map((p) => p.time),
      ["2026-10-09", "2026-10-23"],
    );
    assert.equal(countMissing(points), 0);
  });

  it("keeps the exact decimal string for display", () => {
    const [point] = toChartPoints("month", [obs("2026-08-01", "2026-08-31", "3061245.000")]);
    assert.equal(point.raw.value, "3061245.000");
  });

  it("sorts by time", () => {
    const points = toChartPoints("month", [
      obs("2026-08-01", "2026-08-31", "2"),
      obs("2026-07-01", "2026-07-31", "1"),
    ]);
    assert.deepEqual(
      points.map((p) => p.time),
      ["2026-07-01", "2026-08-01"],
    );
  });
});

describe("summarizePoints", () => {
  it("reports range, latest, low, and high as the exact published observations", () => {
    const points = toChartPoints("business_day", [
      obs("2026-10-26", "2026-10-26", "4.190"),
      obs("2026-10-27", "2026-10-27", "4.050"),
      obs("2026-10-28", "2026-10-28", "4.300"),
      obs("2026-10-29", "2026-10-29", null),
    ]);
    const s = summarizePoints(points);
    assert.equal(s.count, 4);
    assert.equal(s.missing, 1);
    assert.equal(s.first?.period_start, "2026-10-26");
    assert.equal(s.last?.period_start, "2026-10-29");
    assert.equal(s.latest?.value, "4.300"); // the gap is not the latest value
    assert.equal(s.low?.value, "4.050");
    assert.equal(s.high?.value, "4.300");
  });

  it("keeps the earliest of tied extremes and handles empty or all-missing series", () => {
    const tied = summarizePoints(
      toChartPoints("month", [obs("2026-01-01", "2026-01-31", "5"), obs("2026-02-01", "2026-02-28", "5.0")]),
    );
    assert.equal(tied.low?.period_start, "2026-01-01");
    assert.equal(tied.high?.period_start, "2026-01-01");
    assert.deepEqual(summarizePoints([]), {
      count: 0, missing: 0, first: null, last: null, latest: null, low: null, high: null,
    });
    const gaps = summarizePoints(toChartPoints("month", [obs("2026-01-01", "2026-01-31", null)]));
    assert.equal(gaps.latest, null);
    assert.equal(gaps.missing, 1);
  });
});
