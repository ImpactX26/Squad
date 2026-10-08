import type { LucideIcon } from "lucide-react";
import { CheckCircle2, CircleDashed, Flame, Hourglass, Inbox, UserRound } from "lucide-react";

import { CHANNELS, type Channel } from "@/components/ticket/channel";
import type { Schemas } from "@/lib/api";

type TicketRow = Schemas["TicketRow"];

/** GET /api/tickets' filters (§10). The views are fixed combinations of them. */
export type InboxQuery = {
  status?: "open" | TicketRow["status"];
  priority?: TicketRow["priority"];
  channel?: Channel;
  assignee?: "me" | "unassigned";
};

export type View = { id: string; label: string; Icon?: LucideIcon; channel?: Channel; query: InboxQuery };

export const VIEWS: View[] = [
  { id: "open", label: "Open", Icon: Inbox, query: { status: "open" } },
  { id: "mine", label: "Assigned to me", Icon: UserRound, query: { status: "open", assignee: "me" } },
  { id: "unassigned", label: "Unassigned", Icon: CircleDashed, query: { status: "open", assignee: "unassigned" } },
  { id: "urgent", label: "Urgent", Icon: Flame, query: { status: "open", priority: "urgent" } },
  { id: "awaiting", label: "Awaiting customer", Icon: Hourglass, query: { status: "awaiting_customer" } },
  { id: "resolved", label: "Resolved", Icon: CheckCircle2, query: { status: "resolved" } },
];

export const CHANNEL_VIEWS: View[] = (Object.keys(CHANNELS) as Channel[]).map((channel) => ({
  id: channel,
  label: CHANNELS[channel].label,
  channel,
  query: { status: "open", channel },
}));

export const ALL_VIEWS = [...VIEWS, ...CHANNEL_VIEWS];
