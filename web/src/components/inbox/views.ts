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

const PRIORITY_RANK: Record<TicketRow["priority"], number> = { urgent: 0, high: 1, medium: 2, low: 3 };

/** The API's filters, applied to rows in the browser (mock data). */
export function matches(row: TicketRow, query: InboxQuery, me: string): boolean {
  if (query.status === "open" && (row.status === "resolved" || row.status === "closed")) return false;
  if (query.status && query.status !== "open" && row.status !== query.status) return false;
  if (query.priority && row.priority !== query.priority) return false;
  if (query.channel && row.source_channel !== query.channel && !row.channels.includes(query.channel)) return false;
  if (query.assignee === "me" && row.assigned_agent_id !== me) return false;
  if (query.assignee === "unassigned" && row.assigned_agent_id !== null) return false;
  return true;
}

/** The API's order (§10): priority, then most recently updated. */
export function inboxOrder(a: TicketRow, b: TicketRow): number {
  return (
    PRIORITY_RANK[a.priority] - PRIORITY_RANK[b.priority] ||
    Date.parse(b.updated_at) - Date.parse(a.updated_at) ||
    b.ticket_number.localeCompare(a.ticket_number)
  );
}
