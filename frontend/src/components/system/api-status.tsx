import { getApiHealth } from "@/lib/api";

function formatUtc(date: Date): string {
  return `${date.toISOString().replace("T", " ").slice(0, 19)} UTC`;
}

/** Live backend connectivity panel. Must be rendered inside <Suspense>. */
export async function ApiStatus() {
  const result = await getApiHealth();

  if (result.state === "unreachable") {
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
      <dd className="text-fg">{data.environment}</dd>
      <dt className="text-muted">Server time</dt>
      <dd className="text-fg">{formatUtc(new Date(data.server_time_utc))}</dd>
    </dl>
  );
}

export function ApiStatusFallback() {
  return <p className="font-mono text-xs text-muted">Checking backend…</p>;
}
