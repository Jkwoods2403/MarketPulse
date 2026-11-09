import { StateMessage } from "@/components/data/state-message";

/** Body of a module page whose milestone has not started. */
export function ModulePlaceholder({
  milestone,
  summary,
  planned,
}: {
  milestone: string;
  summary: string;
  planned: readonly string[];
}) {
  return (
    <div className="max-w-3xl space-y-3">
      <StateMessage tone="empty" title={`Not operational · planned for ${milestone}`}>
        {summary} This page has no data or functionality yet; it only describes the planned
        scope.
      </StateMessage>
      <div className="border border-border bg-panel p-3">
        <h2 className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">
          Planned scope
        </h2>
        <ul className="grid gap-1 text-xs text-muted sm:grid-cols-2">
          {planned.map((item) => (
            <li key={item} className="flex gap-2">
              <span aria-hidden className="text-faint">
                ›
              </span>
              {item}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
