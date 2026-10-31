import { connection } from "next/server";

/**
 * Server-side client for the Market Pulse FastAPI backend.
 *
 * The browser never calls the backend directly; Next.js server components do (ADR-0004).
 * MARKETPULSE_API_URL has no NEXT_PUBLIC_ prefix, so it is never bundled for the browser.
 *
 * Types are hand-written for now. They will be generated from the API's OpenAPI schema
 * once the API grows (Milestone 2).
 */

const DEFAULT_API_URL = "http://localhost:8000";
const REQUEST_TIMEOUT_MS = 3000;

export type ApiHealth = {
  status: "ok";
  version: string;
  environment: "development" | "test" | "production";
  server_time_utc: string;
};

export type ApiResult<T> =
  | { state: "ok"; data: T; checkedAt: Date }
  | { state: "unreachable"; reason: string; checkedAt: Date };

function apiUrl(path: string): string {
  const base = process.env.MARKETPULSE_API_URL ?? DEFAULT_API_URL;
  return new URL(path, base).toString();
}

/** Check backend liveness. Runs at request time; never cached or prerendered. */
export async function getApiHealth(): Promise<ApiResult<ApiHealth>> {
  await connection();
  const checkedAt = new Date();
  try {
    const response = await fetch(apiUrl("/health"), {
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });
    if (!response.ok) {
      return { state: "unreachable", reason: `HTTP ${response.status}`, checkedAt };
    }
    return { state: "ok", data: (await response.json()) as ApiHealth, checkedAt };
  } catch (error) {
    const reason = error instanceof Error ? error.message : "Unknown error";
    return { state: "unreachable", reason, checkedAt };
  }
}
