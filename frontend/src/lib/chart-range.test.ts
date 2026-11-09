// Run with `pnpm test` (Node's built-in test runner; no test framework dependency).
import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { defaultRange, rangeOptions, rangeStart, type RangeKey } from "./chart-range.ts";

const enabled = (opts: ReturnType<typeof rangeOptions>): RangeKey[] =>
  opts.filter((o) => o.enabled).map((o) => o.key);

describe("rangeStart", () => {
  it("subtracts calendar months from the latest observation", () => {
    assert.equal(rangeStart("1M", "2026-10-30"), "2026-09-30");
    assert.equal(rangeStart("1Y", "2026-10-30"), "2025-10-30");
    assert.equal(rangeStart("5Y", "2026-10-30"), "2021-10-30");
  });

  it("clamps the day to the target month's length", () => {
    assert.equal(rangeStart("1M", "2026-03-31"), "2026-02-28");
    assert.equal(rangeStart("3M", "2024-05-31"), "2024-02-29"); // leap year
  });

  it("crosses year boundaries", () => {
    assert.equal(rangeStart("3M", "2026-01-15"), "2025-10-15");
  });

  it("has no start for Max", () => {
    assert.equal(rangeStart("MAX", "2026-10-30"), null);
  });
});

describe("rangeOptions", () => {
  it("never offers five years when less is stored", () => {
    // Weekly series with history from 2024-01-05 only.
    const opts = rangeOptions("weekly", "2024-01-05", "2026-10-30");
    const fiveYears = opts.find((o) => o.key === "5Y");
    assert.equal(fiveYears?.enabled, false);
    assert.match(fiveYears?.reason ?? "", /Stored history starts 2024-01-05/);
    assert.deepEqual(enabled(opts), ["1M", "3M", "6M", "1Y", "MAX"]);
  });

  it("offers every range when history is long enough", () => {
    assert.deepEqual(enabled(rangeOptions("daily", "1962-01-02", "2026-10-29")), [
      "1M",
      "3M",
      "6M",
      "1Y",
      "5Y",
      "MAX",
    ]);
  });

  it("disables ranges too short to hold two periods", () => {
    assert.deepEqual(enabled(rangeOptions("monthly", "1997-01-01", "2026-08-01")), [
      "3M",
      "6M",
      "1Y",
      "5Y",
      "MAX",
    ]);
    assert.deepEqual(enabled(rangeOptions("quarterly", "1947-01-01", "2026-07-01")), [
      "6M",
      "1Y",
      "5Y",
      "MAX",
    ]);
  });

  it("offers nothing when no observations are stored", () => {
    const opts = rangeOptions("weekly", null, null);
    assert.deepEqual(enabled(opts), []);
    assert.ok(opts.every((o) => o.reason === "No observations stored"));
  });
});

describe("defaultRange", () => {
  it("prefers a frequency-appropriate enabled range", () => {
    assert.equal(defaultRange("daily", rangeOptions("daily", "1962-01-02", "2026-10-29")), "1Y");
    assert.equal(
      defaultRange("weekly", rangeOptions("weekly", "1982-08-20", "2026-10-30")),
      "5Y",
    );
    assert.equal(
      defaultRange("quarterly", rangeOptions("quarterly", "1947-01-01", "2026-07-01")),
      "MAX",
    );
  });

  it("falls back when the preferred range exceeds stored history", () => {
    assert.equal(defaultRange("weekly", rangeOptions("weekly", "2024-01-05", "2026-10-30")), "1Y");
  });
});
