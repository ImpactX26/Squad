/**
 * The dashboard's live feed, WS /ws/staff?token= (§9, §10): every event as {type, data, ts}.
 *
 * Close codes: 1008 sign in again (the session is dropped, so the staff pages go to /login);
 * 1013 this tab fell too far behind: reconnect at once and refetch; anything else (1011, a
 * network drop, a server restart) reconnects with backoff. Events missed while disconnected
 * are not replayed, so every reconnect asks the page to refetch (onResync).
 */

import { WS_URL } from "@/lib/api";
import type { components } from "@/lib/api-types";

export type StaffEvent = components["schemas"]["StaffEvent"];
export type EventType = StaffEvent["type"];
export type FeedStatus = "connecting" | "live" | "reconnecting";

type Options = {
  token: string;
  onEvent: (event: StaffEvent) => void;
  onResync: () => void;
  onSignedOut: () => void;
  onStatus?: (status: FeedStatus) => void;
};

export function connectStaffFeed({ token, onEvent, onResync, onSignedOut, onStatus }: Options): () => void {
  let socket: WebSocket | null = null;
  let attempt = 0;
  let retry: number | undefined;
  let stopped = false;
  let everOpen = false;

  function connect() {
    window.clearTimeout(retry);
    onStatus?.(everOpen ? "reconnecting" : "connecting");
    const ws = new WebSocket(`${WS_URL}/ws/staff?token=${encodeURIComponent(token)}`);
    socket = ws;
    ws.onopen = () => {
      attempt = 0;
      onStatus?.("live");
      // Anything published while this tab was away is not replayed: fetch again.
      if (everOpen) onResync();
      everOpen = true;
    };
    ws.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data) as StaffEvent);
      } catch {
        // a frame we can't read is skipped
      }
    };
    ws.onclose = (event) => {
      if (socket === ws) socket = null;
      if (stopped) return;
      if (event.code === 1008) {
        onSignedOut();
        return;
      }
      const delay =
        event.code === 1013 ? 0 : Math.min(30_000, 1000 * 2 ** attempt) * (0.75 + Math.random() * 0.5);
      attempt += 1;
      onStatus?.("reconnecting");
      retry = window.setTimeout(connect, delay);
    };
  }

  function reconnectNow() {
    if (!socket) {
      attempt = 0;
      connect();
    }
  }

  connect();
  window.addEventListener("online", reconnectNow);
  return () => {
    stopped = true;
    window.removeEventListener("online", reconnectNow);
    window.clearTimeout(retry);
    const ws = socket;
    socket = null;
    if (!ws) return;
    ws.onmessage = null;
    // Closing a socket that is still connecting logs a browser warning: close it once open.
    if (ws.readyState === WebSocket.CONNECTING) ws.onopen = () => ws.close(1000);
    else ws.close(1000);
  };
}
