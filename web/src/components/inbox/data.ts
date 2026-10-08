"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { InboxQuery } from "@/components/inbox/views";
import { useStaffEvents } from "@/components/shell/staff-events";
import { ApiError, api, type Schemas } from "@/lib/api";
import type { FeedStatus } from "@/lib/ws";

type TicketRow = Schemas["TicketRow"];
type TicketList = Schemas["TicketList"];

export const PAGE_SIZE = 100;
// Any of these can change what a view shows or its order (§9).
const TICKET_EVENTS = ["ticket.created", "ticket.updated", "ticket.followup"] as const;
const REFETCH_DEBOUNCE_MS = 300;

export type TicketsState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; tickets: TicketRow[]; total: number };

/** One message of a ticket's conversation, for when GET /api/tickets/{id}/timeline exists. */
export type ConversationMessage = {
  id: string;
  sender_type: "customer" | "ai" | "agent";
  author: string | null;
  channel: TicketRow["source_channel"];
  body: string;
  created_at: string;
};

export type ConversationState =
  | { status: "ready"; messages: ConversationMessage[] }
  | { status: "not_built"; reason: string };

type Loaded = { key: string; state: Exclude<TicketsState, { status: "loading" }> };

/**
 * One view of GET /api/tickets (§10), kept live: any ticket event or a reconnect of /ws/staff
 * refetches it (debounced), so the order is always the server's.
 */
export function useInboxTickets(query: InboxQuery): { state: TicketsState; reload: () => void; live: FeedStatus } {
  const key = new URLSearchParams({ ...query, limit: String(PAGE_SIZE) } as Record<string, string>).toString();
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [version, setVersion] = useState(0);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    api<TicketList>(`/api/tickets?${key}`, { signal: controller.signal })
      .then((list) => setLoaded({ key, state: { status: "ready", tickets: list.tickets, total: list.total } }))
      .catch((error: unknown) => {
        if ((error as Error).name === "AbortError") return;
        const message = error instanceof ApiError ? error.message : "The inbox couldn't be loaded.";
        // A failed refresh keeps the rows already shown; only a first load shows the error.
        setLoaded((previous) =>
          previous?.key === key && previous.state.status === "ready" ? previous : { key, state: { status: "error", message } },
        );
      });
    return () => controller.abort();
  }, [key, version]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const refetchSoon = useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setVersion((v) => v + 1), REFETCH_DEBOUNCE_MS);
  }, []);
  const live = useStaffEvents(TICKET_EVENTS, refetchSoon);

  const state: TicketsState = loaded?.key === key ? loaded.state : { status: "loading" };
  return { state, reload: () => setVersion((v) => v + 1), live };
}

/** The conversation needs GET /api/tickets/{id}/timeline, which is Block 2 (§14.3). */
export function useConversation(ticket: TicketRow): ConversationState {
  void ticket;
  return {
    status: "not_built",
    reason: "It arrives with the ticket timeline in the next round. Until then, the AI summary above says what the customer reported.",
  };
}
