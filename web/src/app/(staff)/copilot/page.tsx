"use client";

// The copilot is now "Any Questions", the sidebar in the staff layout (ARCHITECTURE.md §11.2). This
// old address opens it beside the inbox, so bookmarks and links to /copilot keep working.

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { ErrorState, LoadingState } from "@/components/states";
import { useAnyQuestions } from "@/lib/any-questions";

export default function CopilotPage() {
  const router = useRouter();
  const { available, setOpen } = useAnyQuestions();

  useEffect(() => {
    if (!available) return;
    setOpen(true);
    router.replace("/inbox");
  }, [available, setOpen, router]);

  if (!available) return <ErrorState message="Any Questions is for agents and admins." />;
  return <LoadingState label="Opening Any Questions" rows={2} />;
}
