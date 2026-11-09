import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/**
 * The one status pill of the interface (data status, provenance, module status). Every badge
 * carries a text label, so no status is conveyed by color alone.
 */
export type BadgeTone =
  | "fresh"
  | "stale"
  | "unavailable"
  | "synthetic"
  | "calculated"
  | "planned"
  | "hold"
  | "neutral";

const TONES: Record<BadgeTone, string> = {
  fresh: "border-status-live/40 text-status-live",
  stale: "border-status-stale/40 text-status-stale",
  unavailable: "border-status-unavailable/40 text-status-unavailable",
  synthetic: "border-status-delayed/60 bg-status-delayed/10 text-status-delayed",
  calculated: "border-accent/50 text-accent",
  planned: "border-border-strong text-faint",
  hold: "border-status-delayed/50 text-status-delayed",
  neutral: "border-border-strong text-muted",
};

export function Badge({
  tone,
  children,
  title,
  marker,
  className,
}: {
  tone: BadgeTone;
  children: ReactNode;
  title?: string; // supplementary only: never the sole carrier of information
  marker?: string; // decorative glyph before the label, hidden from screen readers
  className?: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "inline-flex shrink-0 items-center gap-1 rounded-sm border px-1.5 py-px font-mono text-[10px] uppercase leading-4 tracking-wider",
        TONES[tone],
        className,
      )}
    >
      {marker ? <span aria-hidden>{marker}</span> : null}
      {children}
    </span>
  );
}
