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

/** One message of a ticket's conversation, from GET /api/tickets/{id}/timeline. */
export type ConversationMessage = Schemas["TimelineMessage"];
type TicketTimeline = Schemas["TicketTimeline"];

export type ConversationState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; messages: ConversationMessage[] };

// A reply queued, a message added, a follow-up merged: each names its ticket (§9).
const CONVERSATION_EVENTS = ["ticket.updated", "ticket.followup"] as const;

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

type LoadedConversation = { id: string; state: Exclude<ConversationState, { status: "loading" }> };

/**
 * A ticket's conversation across every channel, oldest first (GET /api/tickets/{id}/timeline, §10),
 * kept live: an event about this ticket or a reconnect of /ws/staff refetches it (debounced).
 */
export function useConversation(ticketId: string): { state: ConversationState; reload: () => void } {
  const [loaded, setLoaded] = useState<LoadedConversation | null>(null);
  const [version, setVersion] = useState(0);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => {
    const controller = new AbortController();
    api<TicketTimeline>(`/api/tickets/${encodeURIComponent(ticketId)}/timeline`, { signal: controller.signal })
      .then((timeline) => setLoaded({ id: ticketId, state: { status: "ready", messages: timeline.messages } }))
      .catch((error: unknown) => {
        if ((error as Error).name === "AbortError") return;
        const message = error instanceof ApiError ? error.message : "The conversation couldn't be loaded.";
        // A failed refresh keeps the messages already shown; only a first load shows the error.
        setLoaded((previous) =>
          previous?.id === ticketId && previous.state.status === "ready" ? previous : { id: ticketId, state: { status: "error", message } },
        );
      });
    return () => controller.abort();
  }, [ticketId, version]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  useStaffEvents(CONVERSATION_EVENTS, (event) => {
    if (event && event.data.ticket_id !== ticketId) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setVersion((v) => v + 1), REFETCH_DEBOUNCE_MS);
  });

  const state: ConversationState = loaded?.id === ticketId ? loaded.state : { status: "loading" };
  return { state, reload: () => setVersion((v) => v + 1) };
}
