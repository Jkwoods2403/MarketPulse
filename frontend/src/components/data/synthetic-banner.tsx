import { getApiHealth } from "@/lib/api";

/**
 * Shown on every page while the API serves synthetic development fixtures
 * (`marketpulse serve --synthetic`). Must be rendered inside <Suspense>.
 */
export async function SyntheticBanner() {
  const result = await getApiHealth();
  if (result.state !== "ok" || result.data.data_mode !== "synthetic") return null;
  return (
    <div
      role="status"
      className="border-b border-status-delayed/60 bg-status-delayed/10 px-4 py-1.5 font-mono text-[11px] uppercase tracking-wider text-status-delayed"
    >
      Synthetic development data · generated values, not market data · never shown in production
      builds
    </div>
  );
}
