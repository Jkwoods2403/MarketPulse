import { FreshnessBadge } from "@/components/data/freshness-badge";
import { PRICE_UNAVAILABLE_REASON, UNAVAILABLE_PRICES } from "@/lib/dashboard";

/**
 * Commodity spot prices are deliberately shown as unavailable, with the reason, rather than
 * hidden: a terminal user should see that the gap is a licensing decision, not an outage.
 */
export function UnavailablePricePanel({
  filter,
}: {
  filter?: (label: string) => boolean; // e.g. only natural gas on the Natural Gas page
}) {
  const items = filter ? UNAVAILABLE_PRICES.filter((p) => filter(p.label)) : UNAVAILABLE_PRICES;
  return (
    <div className="space-y-3">
      <ul className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        {items.map((price) => (
          <li
            key={price.label}
            className="flex flex-col gap-2 border border-dashed border-border-strong bg-panel/60 p-3"
          >
            <div className="flex items-start justify-between gap-2">
              <span className="text-xs font-medium text-fg">{price.label}</span>
              <FreshnessBadge status="unavailable" />
            </div>
            <span className="font-mono text-xl leading-7 text-faint" aria-hidden>
              ⊘
            </span>
            <span className="text-[11px] text-muted">
              {price.benchmark} · {price.units}
            </span>
          </li>
        ))}
      </ul>
      <p className="max-w-prose text-[11px] leading-4 text-muted">{PRICE_UNAVAILABLE_REASON}</p>
    </div>
  );
}
