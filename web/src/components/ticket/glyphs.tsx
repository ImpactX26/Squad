import { Cable, Globe, Headphones, Laptop, Mail, MessageCircle, Monitor, Send } from "lucide-react";

import type { SourceChannel, TicketPriority, TicketProduct, TicketStatus } from "@/lib/api";
import { CHANNEL_LABEL, PRIORITY_LABEL, STATUS_LABEL } from "@/lib/format";

const CHANNEL_ICON = {
  web: Globe,
  telegram: Send,
  discord: MessageCircle,
  email: Mail,
} as const;

/** One platform, as a glyph. The label is for screen readers; sighted users get a tooltip. */
export function ChannelGlyph({ channel, className = "size-3.5" }: { channel: SourceChannel; className?: string }) {
  const Icon = CHANNEL_ICON[channel];
  return (
    <span title={CHANNEL_LABEL[channel]} className="inline-flex">
      <Icon className={className} aria-label={CHANNEL_LABEL[channel]} role="img" />
    </span>
  );
}

/**
 * Glyphs stacked in overlapping discs when the conversation crossed platforms (§11.3). A single
 * channel renders as one disc, so rows line up whether or not the ticket moved.
 */
export function ChannelStack({ channels }: { channels: SourceChannel[] }) {
  return (
    <span
      className="inline-flex items-center -space-x-1.5"
      role="group"
      aria-label={`Channels: ${channels.map((c) => CHANNEL_LABEL[c]).join(", ")}`}
    >
      {channels.map((channel, index) => (
        <span
          key={channel}
          style={{ zIndex: channels.length - index }}
          className="grid size-5 place-items-center rounded-full bg-canvas text-ink-secondary ring-2 ring-surface dark:bg-surface-raised"
        >
          <ChannelGlyph channel={channel} className="size-3" />
        </span>
      ))}
    </span>
  );
}

const DEVICE_ICON = {
  laptop: Laptop,
  desktop: Monitor,
  headphones: Headphones,
  accessory: Cable,
} as const;

/** The §11.3 device icon, in its own rounded tile. A ticket with no verified device shows a neutral tile. */
export function DeviceGlyph({ product }: { product: TicketProduct | null }) {
  const Icon = product ? DEVICE_ICON[product.category] : Cable;
  return (
    <span
      className="grid size-10 shrink-0 place-items-center rounded-control bg-canvas text-ink-secondary dark:bg-surface-raised"
      title={product ? product.category : "No verified device"}
    >
      <Icon className="size-5" aria-hidden />
    </span>
  );
}

const PRIORITY_TEXT: Record<TicketPriority, string> = {
  urgent: "text-danger",
  high: "text-warning",
  medium: "text-ink-secondary",
  low: "text-ink-secondary",
};

/** Priority is colour plus a word, never colour alone. */
export function PriorityLabel({ priority }: { priority: TicketPriority }) {
  return (
    <span className={`text-footnote font-medium ${PRIORITY_TEXT[priority]}`}>{PRIORITY_LABEL[priority]}</span>
  );
}

const STATUS_PILL: Record<TicketStatus, string> = {
  new: "bg-accent/12 text-accent",
  in_progress: "bg-accent/12 text-accent",
  awaiting_customer: "bg-warning/15 text-warning",
  awaiting_payment: "bg-warning/15 text-warning",
  scheduled: "bg-success/15 text-success",
  resolved: "bg-canvas text-ink-secondary dark:bg-surface-raised",
  closed: "bg-canvas text-ink-secondary dark:bg-surface-raised",
};

/** Fully rounded status pill (§11.3 shape). */
export function StatusPill({ status }: { status: TicketStatus }) {
  return (
    <span className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-footnote font-medium ${STATUS_PILL[status]}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}
