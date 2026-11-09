"use client";

import { useId } from "react";

import { Button } from "@/components/ui/button";
import type { RangeKey, RangeOption } from "@/lib/chart-range";

/**
 * Date-range buttons. Ranges without enough stored history stay in the tab order but are marked
 * unavailable, with the reason announced to screen readers (and shown on hover), so a five-year
 * button is never offered for a series with less history. While a range loads, the group is
 * busy and further clicks are ignored; focus stays on the pressed button.
 */
export function RangeSelector({
  options,
  active,
  busy,
  onSelect,
}: {
  options: readonly RangeOption[];
  active: RangeKey | null;
  busy: boolean;
  onSelect: (key: RangeKey) => void;
}) {
  const id = useId();
  return (
    <div role="group" aria-label="Date range" aria-busy={busy} className="flex flex-wrap gap-px">
      {options.map((option) => {
        const selected = option.key === active;
        const reasonId = `${id}-${option.key}`;
        return (
          <span key={option.key} className="contents">
            <Button
              variant="segment"
              selected={selected}
              unavailable={!option.enabled}
              aria-pressed={selected}
              aria-describedby={option.enabled ? undefined : reasonId}
              title={option.enabled ? undefined : option.reason}
              className={busy && option.enabled && !selected ? "opacity-60" : undefined}
              onClick={() => {
                if (!busy && !selected) onSelect(option.key);
              }}
            >
              {option.label}
            </Button>
            {option.enabled ? null : (
              <span id={reasonId} className="sr-only">
                Unavailable: {option.reason}
              </span>
            )}
          </span>
        );
      })}
    </div>
  );
}
