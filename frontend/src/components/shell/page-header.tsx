import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import { STATUS_LABELS, type NavStatus } from "@/lib/navigation";

/**
 * Title row of a module page (the page's only h1), with an optional note under it. Modules that
 * are not operational show their status next to the title, in text.
 */
export function PageHeader({
  title,
  description,
  aside,
  status = "active",
}: {
  title: string;
  description?: ReactNode;
  aside?: ReactNode;
  status?: NavStatus;
}) {
  const label = STATUS_LABELS[status];
  return (
    <div className="flex flex-col gap-2 border-b border-border pb-3 md:flex-row md:items-end md:justify-between">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-base font-semibold tracking-tight text-fg">{title}</h1>
          {label.badge ? (
            <Badge tone={status === "blocked" ? "hold" : "planned"} title={label.description}>
              {label.badge}
            </Badge>
          ) : null}
        </div>
        {description ? (
          <p className="mt-1 max-w-3xl text-xs leading-5 text-muted">{description}</p>
        ) : null}
        {label.badge ? <p className="sr-only">{label.description}.</p> : null}
      </div>
      {aside ? <div className="shrink-0">{aside}</div> : null}
    </div>
  );
}
