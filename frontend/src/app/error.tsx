"use client"; // Error boundaries must be Client Components

import { useEffect } from "react";

import { StateMessage } from "@/components/data/state-message";
import { Button } from "@/components/ui/button";

/**
 * Route-level error state. In production, errors from Server Components arrive with a generic
 * message and a digest (never the original details), so only the digest is shown.
 */
export default function RouteError({ error, retry }: { error: unknown; retry: () => void }) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  const digest =
    error instanceof Error && "digest" in error && typeof error.digest === "string"
      ? error.digest
      : undefined;

  return (
    <StateMessage
      tone="error"
      title="This view could not be loaded"
      action={
        <Button onClick={() => retry()}>Try again</Button>
      }
    >
      No data is shown in place of the failed view.
      {digest ? <span className="font-mono text-faint"> Reference: {digest}</span> : null}
    </StateMessage>
  );
}
