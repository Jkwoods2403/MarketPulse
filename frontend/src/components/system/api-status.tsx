import { getApiHealth } from "@/lib/api";
import { environmentLabel } from "@/lib/environment";

function formatUtc(date: Date): string {
  return `${date.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

/** Live backend connectivity panel. Must be rendered inside <Suspense>. */
export async function ApiStatus() {
  const result = await getApiHealth();

  if (result.state !== "ok") {
    return (
      <dl className="grid grid-cols-[8rem_1fr] gap-y-1 font-mono text-xs">
        <dt className="text-muted">Status</dt>
        <dd className="text-down">● Unreachable</dd>
        <dt className="text-muted">Reason</dt>
        <dd className="text-fg">{result.reason}</dd>
        <dt className="text-muted">Checked</dt>
        <dd className="text-fg">{formatUtc(result.checkedAt)}</dd>
        <dt className="text-muted">Hint</dt>
        <dd className="text-muted">
          Start the API: <code className="text-fg">cd backend && uv run marketpulse serve</code>
        </dd>
      </dl>
    );
  }

  const { data } = result;
  return (
    <dl className="grid grid-cols-[8rem_1fr] gap-y-1 font-mono text-xs">
      <dt className="text-muted">Status</dt>
      <dd className="text-status-live">● Connected</dd>
      <dt className="text-muted">API version</dt>
      <dd className="text-fg">{data.version}</dd>
      <dt className="text-muted">Environment</dt>
      <dd className={environmentLabel(data.environment) ? "text-status-delayed" : "text-fg"}>
        {data.environment === "staging" ? "staging (isolated, not production)" : data.environment}
      </dd>
      <dt className="text-muted">Data mode</dt>
      <dd className={data.data_mode === "synthetic" ? "text-status-delayed" : "text-fg"}>
        {data.data_mode === "synthetic" ? "SYNTHETIC fixtures (not market data)" : "Database"}
      </dd>
      <dt className="text-muted">Server time</dt>
      <dd className="text-fg">{formatUtc(new Date(data.server_time_utc))}</dd>
    </dl>
  );
}

export function ApiStatusFallback() {
  return <p className="font-mono text-xs text-muted">Checking backend…</p>;
}

/** One-line backend status for the top bar. Must be rendered inside <Suspense>. */
export async function ApiStatusIndicator() {
  const result = await getApiHealth();
  if (result.state !== "ok") {
    return (
      <span className="text-down" title={`Backend unreachable: ${result.reason}`}>
        ● API unreachable
      </span>
    );
  }
  if (result.data.data_mode === "synthetic") {
    return (
      <span className="text-status-delayed" title="Generated development fixtures, not market data">
        ● API · synthetic data
      </span>
    );
  }
  return (
    <span className="text-status-live" title={`Backend ${result.data.version}`}>
      ● API connected
    </span>
  );
}

/** Names a non-production deployment in the top bar. Must be rendered inside <Suspense>. */
export async function EnvironmentBadge() {
  const result = await getApiHealth();
  if (result.state !== "ok") return null;
  const label = environmentLabel(result.data.environment);
  if (label === null) return null;
  return (
    <span
      className="border border-status-delayed px-1.5 font-semibold text-status-delayed"
      title={`${label} environment: this is not the production deployment`}
    >
      {label}
      <span className="sr-only"> environment: this is not the production deployment.</span>
    </span>
  );
}

export function ApiStatusIndicatorFallback() {
  return <span className="text-faint">● API …</span>;
}
