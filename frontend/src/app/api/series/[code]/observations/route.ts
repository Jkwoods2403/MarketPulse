import { NextResponse, type NextRequest } from "next/server";

import {
  getBackendJson,
  type ApiResult,
  type ChartHistory,
  type ObservationsPage,
} from "@/lib/api";
import type { ChartObservation } from "@/lib/chart-data";
import { defaultRange, rangeOptions, rangeStart } from "@/lib/chart-range";
import {
  BACKEND_PAGE_SIZE,
  MAX_POINTS,
  backendPath,
  parseRequest,
  refuseSynthetic,
} from "@/lib/observations-proxy";

/**
 * Chart history for one series, for the browser's chart components (which never call the
 * backend directly, ADR-0004). A thin read-only proxy over GET /series/{code}/observations:
 * the backend enforces licensing and answers 404 for restricted series and blocked sources.
 *
 * `?range=1M|3M|6M|1Y|5Y|MAX` (default: chosen per frequency). Ranges are anchored on the
 * latest stored observation and limited to the history that exists.
 */
export async function GET(
  request: NextRequest,
  context: RouteContext<"/api/series/[code]/observations">,
) {
  const { code } = await context.params;
  const parsed = parseRequest(code, request.nextUrl.searchParams.get("range"));
  if (!parsed.ok) return error(parsed.status, parsed.error);

  // The oldest stored observation bounds the ranges on offer.
  const head = await getBackendJson<ObservationsPage>(
    backendPath(code, { limit: 1, offset: 0 }),
  );
  if (head.state !== "ok") return backendError(head);
  if (refuseSynthetic(head.data.data_mode, process.env.NODE_ENV)) {
    return error(
      403,
      "The API is serving synthetic development fixtures; they are not shown in production builds",
    );
  }

  const series = head.data.series;
  const earliest = head.data.observations[0]?.period_start ?? null;
  const latest = series.latest?.period_start ?? null;
  const options = rangeOptions(series.frequency, earliest, latest);
  const requested = parsed.range;
  const range =
    requested !== null && options.some((o) => o.key === requested && o.enabled)
      ? requested
      : defaultRange(series.frequency, options);
  const start = latest === null ? null : rangeStart(range, latest);

  const observations: ChartObservation[] = [];
  let total = 0;
  if (earliest !== null) {
    for (let offset = 0; offset < MAX_POINTS; offset += BACKEND_PAGE_SIZE) {
      const page = await getBackendJson<ObservationsPage>(
        backendPath(code, { start, limit: BACKEND_PAGE_SIZE, offset }),
      );
      if (page.state !== "ok") return backendError(page);
      total = page.data.total;
      for (const o of page.data.observations) {
        observations.push({
          period_start: o.period_start,
          period_end: o.period_end,
          source_period: o.source_period,
          value: o.value,
          status: o.status,
        });
      }
      if (observations.length >= total || page.data.observations.length === 0) break;
    }
  }

  const body: ChartHistory = {
    data_mode: head.data.data_mode,
    series,
    range,
    options,
    start,
    earliest,
    latest,
    total,
    truncated: observations.length < total,
    observations,
  };
  return NextResponse.json(body, { headers: { "Cache-Control": "no-store" } });
}

function error(status: number, message: string) {
  return NextResponse.json({ error: message }, { status, headers: { "Cache-Control": "no-store" } });
}

function backendError(result: Exclude<ApiResult<unknown>, { state: "ok" }>) {
  if (result.state === "unreachable") return error(502, "Data API unreachable");
  if (result.status === 404) return error(404, "Series not available");
  if (result.status === 503) return error(503, "Database unavailable");
  return error(502, `Data API error (${result.reason})`);
}
