// Labels and time formatting shared by the inbox row and the ticket page. Sentence case
// throughout (§11.3): no all-caps labels, no eyebrow text.

import type { SourceChannel, TicketPriority, TicketStatus } from "@/lib/api";

export const STATUS_LABEL: Record<TicketStatus, string> = {
  new: "New",
  in_progress: "In progress",
  awaiting_customer: "Awaiting customer",
  awaiting_payment: "Awaiting payment",
  scheduled: "Scheduled",
  resolved: "Resolved",
  closed: "Closed",
};

export const PRIORITY_LABEL: Record<TicketPriority, string> = {
  urgent: "Urgent",
  high: "High",
  medium: "Medium",
  low: "Low",
};

export const CHANNEL_LABEL: Record<SourceChannel | "internal", string> = {
  web: "Web chat",
  telegram: "Telegram",
  discord: "Discord",
  email: "Email",
  internal: "Internal",
};

export const FLAG_LABEL: Record<string, string> = {
  unverified_product: "Unverified device",
  ownership_mismatch: "Ownership mismatch",
  out_of_warranty: "Out of warranty",
};

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["second", 60],
  ["minute", 60],
  ["hour", 24],
  ["day", 7],
  ["week", 4.345],
  ["month", 12],
];

/** "2m ago", "yesterday": the narrow relative style the §11.3 row shows. */
export function relative(iso: string): string {
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "narrow" });
  let delta = (new Date(iso).getTime() - Date.now()) / 1000;
  for (const [unit, span] of UNITS) {
    if (Math.abs(delta) < span) return formatter.format(Math.round(delta), unit);
    delta /= span;
  }
  return formatter.format(Math.round(delta), "year");
}

/** A full timestamp for the timeline's hover title and datetime attribute. */
export function absolute(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** "3 Oct 2026", for warranty dates. */
export function dateOnly(iso: string): string {
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { dateStyle: "medium" });
}
