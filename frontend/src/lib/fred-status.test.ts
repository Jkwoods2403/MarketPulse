// Run with `pnpm test`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  CRUDE_PETROLEUM,
  ECONOMY,
  FRED_SERIES_CODES,
  KEY_INDICATORS,
  METRICS,
  NATURAL_GAS,
  RATES,
  withoutFred,
} from "./dashboard.ts";
import { FRED_LICENSING_STATUS } from "./fred-status.ts";

// Wording that would tell a reader FRED data is being shown, or is current.
const AVAILABILITY_CLAIMS: readonly RegExp[] = [
  /(?<!un)available/i, // "available", but not "unavailable"
  /\blive\b/i,
  /\bcurrent(ly)?\b/i,
  /real[- ]?time/i,
  /up[- ]to[- ]date/i,
  /\blatest\b/i,
  /full revision history/i,
];

function assertNoAvailabilityClaim(text: string, where: string): void {
  for (const claim of AVAILABILITY_CLAIMS) {
    assert.doesNotMatch(text, claim, `${where} implies FRED data is available: ${text}`);
  }
}

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

describe("FRED_LICENSING_STATUS", () => {
  it("labels FRED sections as unavailable for licensing reasons", () => {
    assert.equal(FRED_LICENSING_STATUS.label, "FRED · unavailable (licensing)");
  });

  it("states the reason accurately: written consent for storage, not yet received", () => {
    const { summary } = FRED_LICENSING_STATUS;
    assert.match(summary, /\bunavailable\b/);
    assert.match(summary, /prior written consent/);
    assert.match(summary, /Federal Reserve Bank of St\. Louis/);
    assert.match(summary, /storing it/);
    assert.match(summary, /has not been received/);
    assert.match(FRED_LICENSING_STATUS.note, /unavailable pending FRED licensing consent/i);
  });

  it("points to the documented licensing record", () => {
    assert.equal(FRED_LICENSING_STATUS.reference, "docs/data-sources.md");
    assert.ok(source("../../../docs/data-sources.md").includes("## FRED (Federal Reserve Bank"));
  });

  it("links to the FRED section of that document in the public repository", () => {
    const url = new URL(FRED_LICENSING_STATUS.referenceUrl);
    assert.equal(url.origin, "https://github.com");
    assert.equal(url.pathname, "/Jkwoods2403/MarketPulse/blob/main/docs/data-sources.md");
    // GitHub's anchor for the heading: lowercase, punctuation removed, spaces to hyphens.
    const heading = source("../../../docs/data-sources.md")
      .split("\n")
      .find((line) => line.startsWith("## FRED"));
    assert.ok(heading);
    const slug = heading
      .slice(3)
      .toLowerCase()
      .replace(/[^a-z0-9 -]/g, "")
      .replace(/ /g, "-");
    assert.equal(url.hash, `#${slug}`);
    assert.equal(url.hash, "#fred-federal-reserve-bank-of-st-louis");
  });

  it("explains why no metrics are calculated for FRED series", () => {
    assert.equal(FRED_LICENSING_STATUS.metricsHeading, "Calculated metrics unavailable");
    const { metrics } = FRED_LICENSING_STATUS;
    assert.match(metrics, /only from stored observations/);
    assert.match(metrics, /FRED data is not stored, pending licensing consent/);
    assert.match(metrics, /no changes, ranges, or other metrics are calculated/);
  });

  it("states the Overview's coverage accurately", () => {
    assert.equal(
      FRED_LICENSING_STATUS.indicators,
      "Macroeconomic indicators from FRED are unavailable pending licensing consent.",
    );
  });

  it("never claims FRED data is current, live, or available", () => {
    for (const [key, text] of Object.entries(FRED_LICENSING_STATUS)) {
      assertNoAvailabilityClaim(text, `FRED_LICENSING_STATUS.${key}`);
    }
  });
});

describe("FRED series definitions", () => {
  it("carry no wording that implies FRED data is available", () => {
    for (const metric of [...RATES, ...ECONOMY]) {
      assertNoAvailabilityClaim(`${metric.label} ${metric.note ?? ""}`, metric.code);
    }
  });

  it("state the licensing status where a note describes FRED's own series", () => {
    assert.ok(METRICS.curve10y2y.note?.endsWith(FRED_LICENSING_STATUS.note));
  });

  it("are exactly the 12 FRED series", () => {
    assert.equal(FRED_SERIES_CODES.size, 12);
    for (const metric of [...RATES, ...ECONOMY]) assert.ok(FRED_SERIES_CODES.has(metric.code));
    for (const metric of [...CRUDE_PETROLEUM, ...NATURAL_GAS]) {
      assert.ok(!FRED_SERIES_CODES.has(metric.code), `${metric.code} is not a FRED series`);
    }
  });

  it("withoutFred drops FRED series and keeps every other series, in order", () => {
    const kept = withoutFred(KEY_INDICATORS).map((metric) => metric.code);
    assert.deepEqual(kept, ["us_crude_stocks_ex_spr", "l48_gas_storage", "us_refinery_utilization"]);
    assert.deepEqual(withoutFred(CRUDE_PETROLEUM), CRUDE_PETROLEUM);
    assert.deepEqual(withoutFred(NATURAL_GAS), NATURAL_GAS);
    assert.deepEqual(withoutFred([...RATES, ...ECONOMY]), []);
  });
});

describe("attribution", () => {
  it("is not shown for FRED on the Macro page, which shows no FRED values", () => {
    const text = source("../app/macro/page.tsx");
    assert.doesNotMatch(text, /SourceAttribution/);
    assert.match(text, /<FredLicensingLink \/>/);
  });

  it("on the Overview is unchanged for EIA and never covers FRED series", () => {
    const text = source("../app/page.tsx");
    assert.match(text, /<SourceAttribution metrics=\{CRUDE_CARDS\} \/>/);
    assert.match(text, /<SourceAttribution metrics=\{GAS_CARDS\} \/>/);
    assert.match(text, /<SourceAttribution metrics=\{withoutFred\(KEY_INDICATORS\)\} \/>/);
    assert.doesNotMatch(text, /<SourceAttribution metrics=\{MACRO_CARDS\} \/>/);
    assert.match(text, /footer=\{<FredLicensingNotice \/>\}/);
    const attributed = [...text.matchAll(/<SourceAttribution metrics=\{([^}]+)\} \/>/g)].map(
      (match) => match[1],
    );
    assert.deepEqual(attributed, ["withoutFred(KEY_INDICATORS)", "CRUDE_CARDS", "GAS_CARDS"]);
  });

  it("the licensing link opens the public document safely", () => {
    const text = source("../components/data/fred-licensing.tsx");
    assert.match(text, /href=\{FRED_LICENSING_STATUS\.referenceUrl\}/);
    assert.match(text, /rel="noopener noreferrer"/);
  });
});

describe("pages that show FRED series", () => {
  for (const page of ["../app/macro/page.tsx", "../app/page.tsx"]) {
    it(`${page} labels FRED sections with the licensing status`, () => {
      const text = source(page);
      assert.match(text, /meta=\{FRED_LICENSING_STATUS\.label\}/);
      assert.doesNotMatch(text, /meta="FRED ·/, "a FRED section still has a hard-coded label");
      assert.doesNotMatch(text, /full revision history/);
    });
  }

  it("the Macro page explains the licensing status and links to where it is documented", () => {
    const text = source("../app/macro/page.tsx");
    assert.match(text, /FRED_LICENSING_STATUS\.summary/);
    assert.match(text, /<FredLicensingLink \/>/);
  });

  it("the Macro page shows a licensing notice instead of calculated metrics", () => {
    const text = source("../app/macro/page.tsx");
    // No calculated-metrics panel, heading, provenance label, or calculation footer.
    assert.doesNotMatch(text, /CalculatedMetricsPanel/);
    assert.doesNotMatch(text, /title="Calculated metrics"/);
    assert.doesNotMatch(text, /Derived · current versions/);
    assert.doesNotMatch(text, /Calculated by Market Pulse/);
    // The notice, worded only by the central constant.
    assert.match(text, /title=\{FRED_LICENSING_STATUS\.metricsHeading\}/);
    assert.match(text, /<StateMessage tone="unavailable" title=\{FRED_LICENSING_STATUS\.note\}>/);
    assert.match(text, /\{FRED_LICENSING_STATUS\.metrics\} Licensing details: <FredLicensingLink \/>/);
  });

  it("the EIA pages keep their calculated metrics panels", () => {
    assert.match(source("../app/crude-petroleum/page.tsx"), /<CalculatedMetricsPanel metrics=\{CRUDE_PETROLEUM\} \/>/);
    assert.match(source("../app/natural-gas/page.tsx"), /<CalculatedMetricsPanel metrics=\{NATURAL_GAS\} \/>/);
  });

  it("the Overview no longer implies macroeconomic values are shown", () => {
    const text = source("../app/page.tsx");
    assert.doesNotMatch(text, /macro backdrop/);
    assert.match(text, /FRED_LICENSING_STATUS\.indicators/);
  });
});
