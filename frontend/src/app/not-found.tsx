import Link from "next/link";

import { StateMessage } from "@/components/data/state-message";

export default function NotFound() {
  return (
    <StateMessage
      tone="empty"
      title="Page not found"
      action={
        <Link href="/" className="font-mono text-[11px] uppercase tracking-wider text-accent">
          ‹ Market Overview
        </Link>
      }
    >
      This module or page does not exist.
    </StateMessage>
  );
}
