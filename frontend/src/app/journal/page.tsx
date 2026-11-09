import type { Metadata } from "next";

import { ModulePlaceholder } from "@/components/dashboard/module-placeholder";
import { PageHeader } from "@/components/shell/page-header";

export const metadata: Metadata = { title: "Journal" };

export default function JournalPage() {
  return (
    <div className="space-y-3">
      <PageHeader
        title="Journal"
        status="planned"
        description="The market journal."
      />
      <ModulePlaceholder
        milestone="Milestone 8"
        summary="Record observations and hypotheses with their evidence and expected outcomes, then evaluate them later. Entries are never silently overwritten."
        planned={[
          "Observations, hypotheses, and supporting evidence",
          "Expected outcomes and later evaluation",
          "Tracked edits with full history",
          "Links to the series an entry refers to",
        ]}
      />
    </div>
  );
}
