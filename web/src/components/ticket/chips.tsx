import type { LucideIcon } from "lucide-react";
import { Headphones, Keyboard, Laptop, Monitor, CircleHelp } from "lucide-react";

import type { Schemas } from "@/lib/api";
import { sentence } from "@/lib/format";
import { cn } from "@/lib/utils";

type Status = Schemas["TicketRow"]["status"];
type Priority = Schemas["TicketRow"]["priority"];
type Tone = "accent" | "success" | "warning" | "danger" | "neutral";

// Soft tint behind ink text and a colored dot: readable in both themes (warning text alone
// on white would be too faint).
const TONE: Record<Tone, { soft: string; dot: string }> = {
  accent: { soft: "bg-accent/10", dot: "bg-accent" },
  success: { soft: "bg-success/12", dot: "bg-success" },
  warning: { soft: "bg-warning/14", dot: "bg-warning" },
  danger: { soft: "bg-danger/12", dot: "bg-danger" },
  neutral: { soft: "bg-hairline/50", dot: "bg-ink-secondary" },
};

const STATUS_TONE: Record<Status, Tone> = {
  new: "accent",
  in_progress: "accent",
  awaiting_customer: "warning",
  awaiting_payment: "warning",
  scheduled: "accent",
  resolved: "success",
  closed: "neutral",
};

const PRIORITY_TONE: Record<Priority, Tone> = { urgent: "danger", high: "warning", medium: "neutral", low: "neutral" };

function Pill({ tone, children, className }: { tone: Tone; children: string; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-6 items-center gap-1.5 rounded-full px-2.5 text-footnote font-semibold whitespace-nowrap text-ink",
        TONE[tone].soft,
        className,
      )}
    >
      <span aria-hidden="true" className={cn("size-1.5 rounded-full", TONE[tone].dot)} />
      {children}
    </span>
  );
}

export function StatusChip({ status, className }: { status: Status; className?: string }) {
  return <Pill tone={STATUS_TONE[status]} className={className}>{sentence(status)}</Pill>;
}

export function PriorityChip({ priority, className }: { priority: Priority; className?: string }) {
  return <Pill tone={PRIORITY_TONE[priority]} className={className}>{sentence(priority)}</Pill>;
}

/** The priority as quiet text for list rows: only urgent and high get a dot. */
export function PriorityText({ priority }: { priority: Priority }) {
  const tone = PRIORITY_TONE[priority];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 text-footnote font-semibold",
        priority === "urgent" ? "text-danger" : "text-ink-secondary",
      )}
    >
      {tone !== "neutral" && <span aria-hidden="true" className={cn("size-1.5 rounded-full", TONE[tone].dot)} />}
      {sentence(priority)}
    </span>
  );
}

const DEVICE_ICON: Record<string, LucideIcon> = {
  laptop: Laptop,
  desktop: Monitor,
  headphones: Headphones,
  accessory: Keyboard,
};

/** The row's device icon; a question mark while the device is unverified. */
export function DeviceIcon({ category, className }: { category: string | null | undefined; className?: string }) {
  const Icon = (category && DEVICE_ICON[category]) || CircleHelp;
  return (
    <span className={cn("inline-flex size-9 shrink-0 items-center justify-center rounded-control bg-canvas text-ink-secondary", className)}>
      <Icon aria-hidden="true" className="size-[1.125rem]" />
    </span>
  );
}
