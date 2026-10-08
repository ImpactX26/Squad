import type { ReactNode } from "react";

import { ChannelGlyph, type Channel } from "@/components/ticket/channel";

// NEXT_PUBLIC_* is baked in at build time (§13.3): dev bots on laptops, prod bots on the server.
const TELEGRAM_BOT = (process.env.NEXT_PUBLIC_TELEGRAM_BOT ?? "").replace(/^@/, "");
const DISCORD_INVITE = process.env.NEXT_PUBLIC_DISCORD_INVITE ?? "";
const SUPPORT_EMAIL = process.env.NEXT_PUBLIC_SUPPORT_EMAIL ?? "";

/** Beside the chat: where the serial number is, and the other channels (§11.2). */
export function HelpAside() {
  return (
    <div className="space-y-8">
      <section aria-labelledby="serial">
        <h2 id="serial" className="text-title-2 font-semibold">Find your serial number</h2>
        <p className="mt-1 text-subheadline text-ink-secondary">
          It looks like <span className="font-semibold text-ink tabular-nums">AX14-7F3K92</span>: the model, a dash,
          then six letters and digits.
        </p>
        <dl className="mt-4 divide-y divide-hairline rounded-card bg-surface">
          <Where label="Laptops">On the sticker under the laptop.</Where>
          <Where label="Windows">
            Open Command Prompt and run{" "}
            <code className="inline-block rounded-control bg-canvas px-1.5 py-0.5 font-sans text-footnote font-semibold whitespace-nowrap text-ink">
              wmic bios get serialnumber
            </code>
          </Where>
          <Where label="Desktops">On the sticker on the back or the side panel.</Where>
          <Where label="Headphones">Inside the headband, and on the box.</Where>
        </dl>
      </section>

      <section aria-labelledby="channels">
        <h2 id="channels" className="text-title-2 font-semibold">Other ways to reach us</h2>
        <p className="mt-1 text-subheadline text-ink-secondary">
          The same help everywhere. Write from any of them and your ticket follows you.
        </p>
        <ul className="mt-4 divide-y divide-hairline rounded-card bg-surface">
          <Way channel="telegram" href={TELEGRAM_BOT ? `https://t.me/${TELEGRAM_BOT}` : null}>
            {TELEGRAM_BOT ? `@${TELEGRAM_BOT}` : "Message our support bot."}
          </Way>
          <Way channel="discord" href={DISCORD_INVITE || null}>
            {DISCORD_INVITE ? "Join our support server" : "Write in our support server."}
          </Way>
          <Way channel="email" href={SUPPORT_EMAIL ? `mailto:${SUPPORT_EMAIL}` : null}>
            {SUPPORT_EMAIL || "Email our support team."}
          </Way>
        </ul>
      </section>
    </div>
  );
}

function Where({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="px-4 py-3">
      <dt className="text-subheadline font-semibold text-ink">{label}</dt>
      <dd className="mt-0.5 text-subheadline text-ink-secondary">{children}</dd>
    </div>
  );
}

const NAMES: Record<Exclude<Channel, "web">, string> = { telegram: "Telegram", discord: "Discord", email: "Email" };

function Way({ channel, href, children }: { channel: Exclude<Channel, "web">; href: string | null; children: ReactNode }) {
  const body = (
    <>
      <ChannelGlyph channel={channel} className="size-9 [&_svg]:size-[1.125rem]" />
      <span className="min-w-0">
        <span className="block text-subheadline font-semibold text-ink">{NAMES[channel]}</span>
        <span className={href ? "block truncate text-subheadline text-accent" : "block text-subheadline text-ink-secondary"}>
          {children}
        </span>
      </span>
    </>
  );
  return (
    <li>
      {href ? (
        <a href={href} target={href.startsWith("http") ? "_blank" : undefined} rel="noreferrer" className="flex items-center gap-3 px-4 py-3 hover:bg-canvas/60">
          {body}
        </a>
      ) : (
        <div className="flex items-center gap-3 px-4 py-3">{body}</div>
      )}
    </li>
  );
}
