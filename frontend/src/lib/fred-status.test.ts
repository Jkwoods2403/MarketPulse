// Run with `pnpm test`.
//
// FRED is retired as a data provider (ADR-0015). The dashboard defines, requests, shows, and
// attributes no FRED series; the Macro page and the Overview explain why macroeconomic data is
// missing. Series codes are read from the backend catalog, so these checks follow it exactly.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { describe, it } from "node:test";

import { CRUDE_PETROLEUM, KEY_INDICATORS, METRICS, NATURAL_GAS } from "./dashboard.ts";
import { MACRO_STATUS } from "./fred-status.ts";

function source(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), "utf8");
}

const CATALOG = "../../../backend/src/marketpulse/sources/catalog/data/series/";

/** The `code:` of every series in a backend catalog file. */
function catalogCodes(file: string): Set<string> {
  const text = source(`${CATALOG}${file}`);
  return new Set([...text.matchAll(/^\s*- code: ([a-z0-9_]+)\s*$/gm)].map((match) => match[1]));
}

const FRED_CODES = catalogCodes("fred.yaml");
const EIA_CODES = catalogCodes("eia.yaml");

const DEFINED = [
  ...Object.values(METRICS),
  ...KEY_INDICATORS,
  ...CRUDE_PETROLEUM,
  ...NATURAL_GAS,
].map((metric) => metric.code);

/** Every TypeScript source file of the app, except tests and the generated API types. */
function appSources(): Array<[string, string]> {
  const root = new URL("../", import.meta.url);
  return (readdirSync(root, { recursive: true }) as string[])
    .map((path) => path.replaceAll("\\", "/"))
    .filter((path) => /\.tsx?$/.test(path) && !path.endsWith(".test.ts"))
    .filter((path) => path !== "lib/api-schema.ts")
    .map((path) => [path, readFileSync(new URL(path, root), "utf8")]);
}

describe("active dashboard indicator definitions", () => {
  it("read the real FRED and EIA catalogs", () => {
    assert.equal(FRED_CODES.size, 12);
    assert.ok(FRED_CODES.has("ust_10y") && FRED_CODES.has("cpi_all_items"));
    assert.ok(EIA_CODES.has("l48_gas_storage"));
  });

  it("contain no FRED series code", () => {
    const fred = DEFINED.filter((code) => FRED_CODES.has(code));
    assert.deepEqual(fred, [], `FRED series still defined: ${fred.join(", ")}`);
  });

  it("are all EIA series, the only active approved source", () => {
    const other = DEFINED.filter((code) => !EIA_CODES.has(code));
    assert.deepEqual(other, [], `series from no active source: ${other.join(", ")}`);
  });

  it("keep every EIA indicator the dashboard showed before", () => {
    assert.deepEqual(
      KEY_INDICATORS.map((m) => m.code),
      ["us_crude_stocks_ex_spr", "l48_gas_storage", "us_refinery_utilization"],
    );
    assert.equal(CRUDE_PETROLEUM.length, 6);
    assert.equal(NATURAL_GAS.length, 3);
  });
});

describe("app source", () => {
  it("names no FRED series code anywhere", () => {
    for (const [path, text] of appSources()) {
      for (const code of FRED_CODES) {
        assert.doesNotMatch(text, new RegExp(`["'\`]${code}["'\`]`), `${path} names ${code}`);
      }
    }
  });

  it("carries no outdated FRED licensing wording", () => {
    for (const [path, text] of appSources()) {
      assert.doesNotMatch(text, /licensing consent|consent has not|FRED_LICENSING|withoutFred/i, path);
      assert.doesNotMatch(text, /FredLicensing/, path);
    }
  });
});

describe("MACRO_STATUS", () => {
  it("says FRED is retired and direct sources are planned", () => {
    assert.match(MACRO_STATUS.summary, /FRED is retired as a data provider/);
    assert.match(MACRO_STATUS.summary, /directly from the original publishing organizations/);
    assert.match(MACRO_STATUS.summary, /none is available yet/);
    assert.equal(MACRO_STATUS.label, "Not available · direct sources planned");
    assert.equal(MACRO_STATUS.indicators, "Macroeconomic indicators are not available yet.");
  });

  it("never implies macroeconomic data is shown, current, or pending consent", () => {
    for (const [key, text] of Object.entries(MACRO_STATUS)) {
      for (const claim of [/\blive\b/i, /real[- ]?time/i, /up[- ]to[- ]date/i, /\blatest\b/i]) {
        assert.doesNotMatch(text, claim, `MACRO_STATUS.${key}: ${text}`);
      }
      assert.doesNotMatch(text, /consent|pending/i, `MACRO_STATUS.${key}: ${text}`);
    }
  });

  it("links to ADR-0015, which exists in the repository", () => {
    assert.equal(MACRO_STATUS.reference, "ADR-0015");
    const url = new URL(MACRO_STATUS.referenceUrl);
    assert.equal(url.host, "github.com");
    const path = url.pathname.replace("/Jkwoods2403/MarketPulse/blob/main/", "");
    assert.equal(path, "docs/adr/0015-direct-source-data-only.md");
    assert.match(source(`../../../${path}`), /^# ADR-0015/m);
  });

  it("is linked safely", () => {
    const text = source("../components/data/macro-status.tsx");
    assert.match(text, /href=\{MACRO_STATUS\.referenceUrl\}/);
    assert.match(text, /rel="noopener noreferrer"/);
  });
});

describe("pages", () => {
  it("the Macro page is planned and shows only the status message", () => {
    const text = source("../app/macro/page.tsx");
    assert.match(text, /status="planned"/);
    assert.match(text, /<MacroStatusMessage \/>/);
    assert.match(text, /meta=\{MACRO_STATUS\.label\}/);
    for (const component of [/SeriesCards/, /TimeSeriesChart/, /CalculatedMetricsPanel/]) {
      assert.doesNotMatch(text, component);
    }
    assert.doesNotMatch(text, /SourceAttribution/);
  });

  it("the Overview explains the missing macro data and attributes only EIA cards", () => {
    const text = source("../app/page.tsx");
    assert.match(text, /MACRO_STATUS\.indicators/);
    assert.match(text, /<MacroStatusMessage \/>/);
    const attributed = [...text.matchAll(/<SourceAttribution metrics=\{([^}]+)\} \/>/g)].map(
      (match) => match[1],
    );
    assert.deepEqual(attributed, ["KEY_INDICATORS", "CRUDE_CARDS", "GAS_CARDS"]);
  });

  it("the EIA pages keep their calculated metrics panels", () => {
    assert.match(
      source("../app/crude-petroleum/page.tsx"),
      /<CalculatedMetricsPanel metrics=\{CRUDE_PETROLEUM\} \/>/,
    );
    assert.match(
      source("../app/natural-gas/page.tsx"),
      /<CalculatedMetricsPanel metrics=\{NATURAL_GAS\} \/>/,
    );
  });
});
