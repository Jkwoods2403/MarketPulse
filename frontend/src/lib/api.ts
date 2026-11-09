import { connection } from "next/server";
import { cache } from "react";

import type { components } from "@/lib/api-schema";
import type { ChartObservation } from "@/lib/chart-data";
import type { RangeKey, RangeOption } from "@/lib/chart-range";

/**
 * Server-side client for the Market Pulse FastAPI backend.
 *
 * The browser never calls the backend directly; Next.js server components do (ADR-0004).
 * MARKETPULSE_API_URL has no NEXT_PUBLIC_ prefix, so it is never bundled for the browser.
 *
 * Response types are generated from the backend's OpenAPI schema (src/lib/api-schema.ts, from
 * backend/openapi.json; `pnpm api:types` regenerates, `pnpm api:check` fails if stale), so the
 * frontend and backend contracts cannot drift silently. Market values are exact decimal
 * strings, never floats. Licensing is enforced by the backend: restricted series and blocked
 * sources never reach these responses.
 */

const DEFAULT_API_URL = "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 3000;

type Schemas = components["schemas"];

export type DataMode = Schemas["HealthResponse"]["data_mode"];
export type ApiHealth = Schemas["HealthResponse"];
export type Freshness = Schemas["Freshness"];
export type FreshnessStatus = Freshness["status"];
export type SourceInfo = Schemas["SourceInfo"];
export type LatestObservation = Schemas["LatestObservation"];
export type SeriesSummary = Schemas["SeriesSummary"];
export type SeriesList = Schemas["SeriesListResponse"];
/** One page of GET /series/{code}/observations. */
export type ObservationsPage = Schemas["ObservationsResponse"];
/** Body of a backend 404 or 503 answer. */
export type ApiErrorBody = Schemas["ErrorResponse"];

export type ApiResult<T> =
  | { state: "ok"; data: T; checkedAt: Date }
  | { state: "unreachable"; reason: string; checkedAt: Date }
  | { state: "error"; reason: string; checkedAt: Date; status?: number };

function apiUrl(path: string): string {
  const base = process.env.MARKETPULSE_API_URL ?? DEFAULT_API_URL;
  return new URL(path, base).toString();
}

/** GET a backend path from the server. Never cached; never runs in the browser. */
export async function getBackendJson<T>(path: string): Promise<ApiResult<T>> {
  await connection();
  const checkedAt = new Date();
  let response: Response;
  try {
    response = await fetch(apiUrl(path), {
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
  } catch (error) {
    const reason = error instanceof Error ? error.message : "Unknown error";
    return { state: "unreachable", reason, checkedAt };
  }
  if (!response.ok) {
    const reason =
      response.status === 503 ? "Database unavailable (HTTP 503)" : `HTTP ${response.status}`;
    return { state: "error", reason, checkedAt, status: response.status };
  }
  try {
    return { state: "ok", data: (await response.json()) as T, checkedAt };
  } catch {
    return { state: "error", reason: "Response was not valid JSON", checkedAt };
  }
}

/**
 * Check backend liveness and data mode. Runs at request time (never prerendered); deduplicated
 * per request, since the top bar, banner, and system panel all read it.
 */
export const getApiHealth = cache(async (): Promise<ApiResult<ApiHealth>> => {
  const result = await getBackendJson<ApiHealth>("/health");
  return result.state === "error" ? { ...result, state: "unreachable" } : result;
});

function isSeriesList(value: unknown): value is SeriesList {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Partial<SeriesList>;
  return (
    (v.data_mode === "database" || v.data_mode === "synthetic") &&
    typeof v.generated_at === "string" &&
    Array.isArray(v.series)
  );
}

/**
 * Every permitted series with its latest observation and freshness. Deduplicated per request,
 * so all sections of a page share one backend call.
 *
 * Synthetic development fixtures are refused in production builds: they are generated values,
 * never market data, and must not reach a production view.
 */
export const getSeriesList = cache(async (): Promise<ApiResult<SeriesList>> => {
  const result = await getBackendJson<unknown>("/series");
  if (result.state !== "ok") return result;
  if (!isSeriesList(result.data)) {
    return { state: "error", reason: "Unexpected response from /series", checkedAt: result.checkedAt };
  }
  if (result.data.data_mode === "synthetic" && process.env.NODE_ENV === "production") {
    return {
      state: "error",
      reason: "The API is serving synthetic development fixtures; they are not shown in production builds",
      checkedAt: result.checkedAt,
    };
  }
  return { state: "ok", data: result.data, checkedAt: result.checkedAt };
});

/** What the chart route handler (app/api/series/[code]/observations) returns. */
export type ChartHistory = {
  data_mode: DataMode;
  series: SeriesSummary;
  range: RangeKey;
  options: RangeOption[];
  start: string | null; // first period_start requested (null for Max)
  earliest: string | null; // oldest stored period_start
  latest: string | null; // newest period_start with a value
  total: number; // observations in the range, as counted by the backend
  truncated: boolean; // true if more than MAX_POINTS were available
  observations: ChartObservation[];
};

/** Body of an error answer from the chart route handler. */
export type ChartRouteError = { error: string };

// --- Calculated metrics (Phase 4) -------------------------------------------------------------

export type ObservationRef = Schemas["ObservationRef"];
/** A value derived by Market Pulse, never published by the source. */
export type CalculatedMetric = Schemas["CalculatedMetric"];
export type MetricKind = CalculatedMetric["kind"];
export type MetricStatus = CalculatedMetric["status"];
export type SeriesMetrics = Schemas["SeriesMetrics"];
export type MetricsList = Schemas["MetricsResponse"];

function isMetricsList(value: unknown): value is MetricsList {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Partial<MetricsList>;
  return (
    (v.data_mode === "database" || v.data_mode === "synthetic") &&
    typeof v.generated_at === "string" &&
    Array.isArray(v.series) &&
    v.series.every((s) => s.calculated === true && s.basis === "current_versions")
  );
}

/**
 * Calculated metrics for every permitted series (GET /metrics). Deduplicated per request.
 * Synthetic fixtures are refused in production builds, as for getSeriesList.
 */
export const getMetrics = cache(async (): Promise<ApiResult<MetricsList>> => {
  const result = await getBackendJson<unknown>("/metrics");
  if (result.state !== "ok") return result;
  if (!isMetricsList(result.data)) {
    return { state: "error", reason: "Unexpected response from /metrics", checkedAt: result.checkedAt };
  }
  if (result.data.data_mode === "synthetic" && process.env.NODE_ENV === "production") {
    return {
      state: "error",
      reason: "The API is serving synthetic development fixtures; they are not shown in production builds",
      checkedAt: result.checkedAt,
    };
  }
  return { state: "ok", data: result.data, checkedAt: result.checkedAt };
});
