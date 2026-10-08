import type { Metadata } from "next";

import { EmptyState } from "@/components/states";

export const metadata: Metadata = { title: "Inbox" };

// The three-pane inbox is the next step (§14.3, P3); until then the shell says so.
export default function InboxPage() {
  return (
    <EmptyState title="The inbox isn't built yet" className="flex-1">
      The ticket list and preview come in the next step.
    </EmptyState>
  );
}
