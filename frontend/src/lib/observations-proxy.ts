/**
 * Request validation for the chart history route handler (app/api/series/[code]/observations).
 * Pure, with only a type import (see chart-range.ts).
 *
 * The handler is a thin, read-only proxy: licensing is enforced by the backend, which answers
 * 404 for restricted series and blocked sources (ERCOT). This module only makes sure nothing
 * but a well-formed series code and a known range ever reaches the backend URL.
 */

import type { RangeKey } from "./chart-range";

// Same rule as the backend catalog (lowercase, starts with a letter).
const SERIES_CODE = /^[a-z][a-z0-9_]{1,62}$/;
const RANGE_KEYS: readonly string[] = ["1M", "3M", "6M", "1Y", "5Y", "MAX"];

export const BACKEND_PAGE_SIZE = 5000; // the backend's maximum `limit`
export const MAX_POINTS = 50_000; // a chart never requests more than this

export type ParsedRequest =
  | { ok: true; code: string; range: RangeKey | null }
  | { ok: false; status: 400; error: string };

export function parseRequest(code: string, rangeParam: string | null): ParsedRequest {
  if (!SERIES_CODE.test(code)) return { ok: false, status: 400, error: "invalid series code" };
  if (rangeParam !== null && !RANGE_KEYS.includes(rangeParam)) {
    return { ok: false, status: 400, error: "invalid range" };
  }
  return { ok: true, code, range: rangeParam as RangeKey | null };
}

/** Path on the backend; the code has been validated, and parameters are URL-encoded. */
export function backendPath(
  code: string,
  params: { start?: string | null; limit: number; offset: number },
): string {
  const query = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) });
  if (params.start) query.set("start", params.start);
  return `/series/${code}/observations?${query.toString()}`;
}

/** Synthetic fixtures are never served to a production build. */
export function refuseSynthetic(dataMode: string, nodeEnv: string | undefined): boolean {
  return dataMode === "synthetic" && nodeEnv === "production";
}
