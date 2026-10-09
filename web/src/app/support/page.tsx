// The customer's page (§11.2, §11.4): the website chat, plus where to find a serial number and the other
// channels, in the welcome screen's frame. Public, no account. Escape (or "Home") goes back to the home page.
//
// The chat only ever reaches the customer intake pipeline (§4.1): catalog, tickets, knowledge and
// messaging. Payments, dispatch and stock are not reachable from here, by code on the backend.

import { ArrowLeft, Laptop, Terminal } from "lucide-react";
import Link from "next/link";

import { LogoMark } from "@/components/brand";
import { ChatPanel } from "@/components/chat-widget/chat-panel";
import { EscapeHome } from "@/components/escape-home";
import { ChannelGlyph } from "@/components/ticket/glyphs";
import { ThemeToggle } from "@/components/theme-toggle";

export const metadata = { title: "Chat with support" };

export default function SupportPage() {
  return (
    <div className="grid min-h-dvh place-items-center px-3 py-4 sm:px-6 sm:py-10">
      <EscapeHome />
      <div className="grid w-full max-w-[1180px] gap-3 rounded-[40px] bg-frost p-3 shadow-raised ring-1 ring-white/60 backdrop-blur-xl sm:gap-4 sm:p-4 lg:grid-cols-[0.9fr_1.1fr] dark:ring-white/8">
        <section className="flex flex-col rounded-panel bg-[linear-gradient(160deg,#eee9f9_0%,#f1e8f4_55%,#f8e6ea_100%)] px-6 pt-6 pb-8 sm:px-10 sm:pt-10 dark:bg-[linear-gradient(160deg,#1d1838,#211631)]">
          <div className="flex items-center justify-between gap-3">
            <Link
              href="/"
              className="inline-flex items-center gap-2 rounded-full bg-white/70 px-3 py-1.5 text-[14px] font-semibold text-ink outline-none transition hover:bg-white focus-visible:ring-2 focus-visible:ring-violet dark:bg-white/8 dark:hover:bg-white/12"
            >
              <ArrowLeft className="size-4" aria-hidden /> Home
              <kbd className="hidden rounded-md bg-ink/8 px-1.5 text-[12px] font-medium text-ink-secondary sm:inline">Esc</kbd>
            </Link>
            <ThemeToggle />
          </div>

          <div className="mt-10 flex items-center gap-3">
            <LogoMark className="size-11 drop-shadow-[0_4px_10px_rgb(44_27_100/0.12)]" />
            <span className="text-[22px] font-bold tracking-[-0.03em] text-ink">ServiceMesh</span>
          </div>
          <h1 className="mt-8 text-[34px] leading-[1.08] font-bold tracking-[-0.04em] text-ink sm:text-[40px]">
            How can we help?
          </h1>
          <p className="mt-4 max-w-[42ch] text-[16px] leading-relaxed text-ink-secondary">
            Tell us what&apos;s wrong with your laptop, desktop or headphones. Add the serial number and we can raise
            a ticket straight away; we&apos;ll reply right here.
          </p>

          <div className="mt-8 space-y-3 rounded-card bg-surface/80 p-5 shadow-card">
            <h2 className="text-[16px] font-bold tracking-[-0.01em] text-ink">Where&apos;s my serial number?</h2>
            <ul className="space-y-2.5 text-[14px] text-ink-secondary">
              <li className="flex gap-2.5">
                <Laptop className="mt-0.5 size-4 shrink-0 text-violet" aria-hidden />
                <span>
                  On the sticker under the laptop or on the back of the desktop, e.g.{" "}
                  <span className="font-semibold whitespace-nowrap tabular-nums text-ink">AX14-7F3K92</span>.
                </span>
              </li>
              <li className="flex gap-2.5">
                <Terminal className="mt-0.5 size-4 shrink-0 text-violet" aria-hidden />
                <span>
                  On Windows, run{" "}
                  <code className="rounded-md bg-canvas px-1.5 py-0.5 text-[13px] text-ink dark:bg-surface-raised">
                    wmic bios get serialnumber
                  </code>{" "}
                  in Command Prompt.
                </span>
              </li>
            </ul>
          </div>

          <p className="mt-8 flex flex-wrap items-center gap-2 text-[14px] text-ink-secondary lg:mt-auto lg:pt-8">
            Or write to us on
            {(["discord", "telegram", "email"] as const).map((channel) => (
              <span key={channel} className="inline-flex items-center gap-1.5 rounded-full bg-white/70 px-2.5 py-1 font-medium text-ink dark:bg-white/8">
                <ChannelGlyph channel={channel} /> {channel === "email" ? "Email" : channel[0].toUpperCase() + channel.slice(1)}
              </span>
            ))}
            <span className="w-full">It&apos;s the same ticket wherever you write.</span>
          </p>
        </section>

        <div className="min-h-[560px] [&>*]:h-full">
          <ChatPanel />
        </div>
      </div>
    </div>
  );
}
