import type { ButtonHTMLAttributes } from "react";

import { cn } from "@/lib/cn";

type Variant = "default" | "segment";

const VARIANTS: Record<Variant, string> = {
  default:
    "h-7 border-border-strong bg-panel-raised px-2.5 text-[11px] text-fg hover:border-accent",
  segment: "h-6 min-w-8 px-1.5 text-[10px]",
};

/**
 * Shared button. `unavailable` keeps the button focusable and announced (aria-disabled) instead
 * of removing it from the tab order, so its reason (via aria-describedby) stays reachable by
 * keyboard; clicks are ignored while it is set.
 */
export function Button({
  variant = "default",
  selected = false,
  unavailable = false,
  className,
  onClick,
  type = "button",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  selected?: boolean;
  unavailable?: boolean;
}) {
  return (
    <button
      type={type}
      aria-disabled={unavailable || undefined}
      onClick={unavailable ? (event) => event.preventDefault() : onClick}
      className={cn(
        "inline-flex items-center justify-center border font-mono uppercase tracking-wider transition-colors",
        VARIANTS[variant],
        variant === "segment" &&
          (selected
            ? "border-accent bg-accent/15 text-fg"
            : "border-border bg-panel text-muted hover:border-border-strong hover:text-fg"),
        unavailable &&
          "cursor-not-allowed border-border text-faint line-through hover:border-border hover:text-faint",
        className,
      )}
      {...props}
    />
  );
}
