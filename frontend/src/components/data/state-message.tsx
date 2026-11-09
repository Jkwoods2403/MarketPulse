import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

type Tone = "empty" | "error" | "unavailable";

const TONES: Record<Tone, { marker: string; className: string }> = {
  empty: { marker: "○", className: "text-muted" },
  unavailable: { marker: "⊘", className: "text-status-unavailable" },
  error: { marker: "▲", className: "text-down" },
};

/** One consistent message block for empty, unavailable, and error states. */
export function StateMessage({
  tone,
  title,
  children,
  action,
  className,
}: {
  tone: Tone;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  const { marker, className: toneClass } = TONES[tone];
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn("flex gap-3 border border-border bg-panel px-3 py-2.5", className)}
    >
      <span aria-hidden className={cn("font-mono text-sm leading-5", toneClass)}>
        {marker}
      </span>
      <div className="min-w-0 space-y-1">
        <p className={cn("text-xs font-semibold leading-5", toneClass)}>{title}</p>
        {children ? <div className="text-xs leading-5 text-muted">{children}</div> : null}
        {action ? <div className="pt-1">{action}</div> : null}
      </div>
    </div>
  );
}

/** A pulsing placeholder block for loading states. */
export function Skeleton({ className }: { className?: string }) {
  return <div aria-hidden className={cn("animate-pulse rounded-sm bg-panel-raised motion-reduce:animate-none", className)} />;
}
