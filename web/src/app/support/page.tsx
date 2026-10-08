import type { Metadata } from "next";

import { Brand } from "@/components/brand";
import { ChatPanel } from "@/components/chat-widget/chat-panel";
import { HelpAside } from "@/components/chat-widget/help-aside";
import { HomeLink } from "@/components/home-link";
import { ThemeToggle } from "@/components/theme-toggle";

export const metadata: Metadata = { title: "Support" };

// The customer's way in (§11.2): no account. It calls only POST /api/chat/session and
// WS /ws/chat/{session_id}, whose messages run the customer intake (§4.1).
export default function SupportPage() {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between gap-3 px-4 sm:px-6">
        <HomeLink className="-ml-2" />
        <Brand className="hidden sm:inline-flex" />
        <ThemeToggle />
      </header>

      <main className="mx-auto grid w-full max-w-6xl flex-1 gap-8 px-2 pb-10 sm:px-6 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:gap-10">
        <div className="min-w-0 lg:sticky lg:top-4 lg:self-start">
          <h1 className="mb-3 px-2 text-title-1 font-semibold sm:px-0">How can we help?</h1>
          <ChatPanel className="h-[calc(100dvh-7.5rem)] max-h-[44rem] min-h-[28rem]" />
        </div>
        <aside className="min-w-0 px-2 sm:px-0 lg:pt-14">
          <HelpAside />
        </aside>
      </main>
    </div>
  );
}
