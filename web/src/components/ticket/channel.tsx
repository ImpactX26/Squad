import type { LucideIcon } from "lucide-react";
import { Gamepad2, Globe, Mail, Send } from "lucide-react";

import type { Schemas } from "@/lib/api";
import { cn } from "@/lib/utils";

export type Channel = Schemas["TicketRow"]["source_channel"];

/** Each channel's name, icon and tint (§11.3 channel-* tokens). Full class names, for Tailwind. */
export const CHANNELS: Record<Channel, { label: string; Icon: LucideIcon; text: string; soft: string }> = {
  telegram: { label: "Telegram", Icon: Send, text: "text-channel-telegram", soft: "bg-channel-telegram/12" },
  discord: { label: "Discord", Icon: Gamepad2, text: "text-channel-discord", soft: "bg-channel-discord/12" },
  email: { label: "Email", Icon: Mail, text: "text-channel-email", soft: "bg-channel-email/12" },
  web: { label: "Web chat", Icon: Globe, text: "text-channel-web", soft: "bg-channel-web/12" },
};

/** The channel's icon in a round tinted chip. */
export function ChannelGlyph({ channel, className }: { channel: Channel; className?: string }) {
  const { label, Icon, text, soft } = CHANNELS[channel];
  return (
    <span
      title={label}
      className={cn("inline-flex size-6 shrink-0 items-center justify-center rounded-full", soft, text, className)}
    >
      <Icon aria-hidden="true" className="size-3.5" />
      <span className="sr-only">{label}</span>
    </span>
  );
}

/** Glyphs overlapping: the conversation crossed these platforms, first one first (§11.3). */
export function ChannelStack({ channels, className }: { channels: Channel[]; className?: string }) {
  return (
    <span className={cn("inline-flex items-center", className)}>
      {channels.map((channel, i) => (
        <ChannelGlyph key={channel} channel={channel} className={cn("ring-2 ring-surface", i > 0 && "-ml-1.5")} />
      ))}
    </span>
  );
}

/** Icon and name, as a pill. */
export function ChannelBadge({ channel, className }: { channel: Channel; className?: string }) {
  const { label, Icon, text, soft } = CHANNELS[channel];
  return (
    <span
      className={cn(
        "inline-flex h-7 items-center gap-1.5 rounded-full pr-3 pl-2 text-footnote font-semibold",
        soft,
        text,
        className,
      )}
    >
      <Icon aria-hidden="true" className="size-3.5" />
      {label}
    </span>
  );
}
