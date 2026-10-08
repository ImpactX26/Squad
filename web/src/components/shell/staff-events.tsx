"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useNotifications } from "@/components/shell/notifications";
import { clearSession } from "@/lib/auth";
import { connectStaffFeed, type EventType, type FeedStatus, type StaffEvent } from "@/lib/ws";

type Listener = { types: readonly EventType[] | "resync"; handler: (event: StaffEvent | null) => void };

type StaffEventsValue = { status: FeedStatus; listen: (listener: Listener) => () => void };

const StaffEventsContext = createContext<StaffEventsValue | null>(null);

/** One /ws/staff connection per tab, shared by every page in the shell; it also feeds the bell. */
export function StaffEventsProvider({ token, staffId, children }: { token: string; staffId: string; children: ReactNode }) {
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const listeners = useRef(new Set<Listener>());
  const { add } = useNotifications();

  useEffect(() => {
    return connectStaffFeed({
      token,
      onStatus: setStatus,
      onSignedOut: clearSession, // 1008: the shell sends the page to /login
      onResync: () => {
        for (const l of listeners.current) if (l.types === "resync") l.handler(null);
      },
      onEvent: (event) => {
        if (event.type === "notification.created" && event.data.user_id === staffId) {
          add({
            id: String(event.data.notification_id),
            title: String(event.data.title ?? ""),
            body: typeof event.data.body === "string" ? event.data.body : null,
            link: typeof event.data.link === "string" ? event.data.link : null,
            createdAt: typeof event.data.created_at === "string" ? event.data.created_at : event.ts,
          });
        }
        for (const l of listeners.current) if (l.types !== "resync" && l.types.includes(event.type)) l.handler(event);
      },
    });
  }, [token, staffId, add]);

  const listen = useCallback((listener: Listener) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);
  const value = useMemo(() => ({ status, listen }), [status, listen]);

  return <StaffEventsContext.Provider value={value}>{children}</StaffEventsContext.Provider>;
}

function useStaffEventsContext(): StaffEventsValue {
  const value = useContext(StaffEventsContext);
  if (!value) throw new Error("staff events need a StaffEventsProvider");
  return value;
}

/** Call handler for each event of these types, and with null after a reconnect (refetch then). */
export function useStaffEvents(types: readonly EventType[], handler: (event: StaffEvent | null) => void): FeedStatus {
  const { status, listen } = useStaffEventsContext();
  const latest = useRef(handler);
  useEffect(() => {
    latest.current = handler;
  }, [handler]);
  const key = types.join(",");

  useEffect(() => {
    const wanted = key.split(",") as EventType[];
    const offEvents = listen({ types: wanted, handler: (event) => latest.current(event) });
    const offResync = listen({ types: "resync", handler: () => latest.current(null) });
    return () => {
      offEvents();
      offResync();
    };
  }, [key, listen]);

  return status;
}
