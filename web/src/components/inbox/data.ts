"use client";

import { useMemo } from "react";

import { inboxOrder, matches, type InboxQuery } from "@/components/inbox/views";
import type { Schemas } from "@/lib/api";
import { mockConversation, mockTickets, type MockMessage } from "@/mocks/inbox";

type TicketRow = Schemas["TicketRow"];

export type TicketsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tickets: TicketRow[]; total: number };

export type ConversationState =
  | { status: "ready"; messages: MockMessage[] }
  | { status: "not_built"; reason: string };

/** The inbox's tickets for one view. Mock data for now, shaped like GET /api/tickets (§14.6). */
export function useInboxTickets(query: InboxQuery, me: string): { state: TicketsState; reload: () => void } {
  const state = useMemo<TicketsState>(() => {
    const tickets = mockTickets(me).filter((row) => matches(row, query, me)).sort(inboxOrder);
    return { status: "ready", tickets, total: tickets.length };
  }, [query, me]);
  return { state, reload: () => {} };
}

export function useConversation(ticket: TicketRow): ConversationState {
  return useMemo(() => ({ status: "ready", messages: mockConversation(ticket) }), [ticket]);
}
