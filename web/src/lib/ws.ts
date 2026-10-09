// Dashboard realtime (ARCHITECTURE.md §9): every bus event arrives on /ws/staff as
// {type, data, ts}. The socket reconnects with backoff, because a dropped connection
// during a demo must heal itself without a page reload.

import { env } from "@/lib/env";
import { getToken } from "@/lib/api";

/** The §9 event names the dashboard listens for. */
export type StaffEventType =
  | "message.received"
  | "message.sent"
  | "ticket.created"
  | "ticket.updated"
  | "ticket.followup"
  | "agent.tool_called"
  | "payment.link_sent"
  | "payment.utr_submitted"
  | "payment.paid"
  | "payment.failed"
  | "job.assigned"
  | "job.status_changed"
  | "job.completed"
  | "job.rejected"
  | "stock.low"
  | "notification.created";

export type StaffEvent = {
  type: StaffEventType;
  data: Record<string, unknown>;
  ts: string;
};

export type SocketStatus = "connecting" | "live" | "offline";

const RECONNECT_DELAYS_MS = [500, 1000, 2000, 5000, 10000];

/**
 * Subscribe to /ws/staff. Returns the unsubscribe function, which closes the socket and
 * stops any pending reconnect.
 */
export function openStaffSocket(
  onEvent: (event: StaffEvent) => void,
  onStatus?: (status: SocketStatus) => void,
): () => void {
  let socket: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let attempt = 0;
  let closed = false;

  function connect() {
    if (closed) return;
    const token = getToken();
    if (!token) {
      onStatus?.("offline");
      return;
    }
    onStatus?.(attempt === 0 ? "connecting" : "connecting");
    socket = new WebSocket(`${env.wsUrl}/ws/staff?token=${encodeURIComponent(token)}`);

    socket.onopen = () => {
      attempt = 0;
      onStatus?.("live");
    };
    socket.onmessage = (message) => {
      try {
        onEvent(JSON.parse(message.data as string) as StaffEvent);
      } catch {
        // A frame we can't parse is not worth tearing the socket down for.
      }
    };
    socket.onclose = () => {
      socket = null;
      if (closed) return;
      onStatus?.("offline");
      const delay = RECONNECT_DELAYS_MS[Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)];
      attempt += 1;
      timer = setTimeout(connect, delay);
    };
    // onclose always follows onerror, so the reconnect is handled in one place.
    socket.onerror = () => socket?.close();
  }

  connect();

  return () => {
    closed = true;
    if (timer) clearTimeout(timer);
    // Closing a socket that is still connecting makes the browser log a warning, with the token
    // in the URL; React's dev double mount does exactly that. Let it connect, then close it.
    const pending = socket;
    if (pending?.readyState === WebSocket.CONNECTING) {
      pending.onmessage = null;
      pending.onopen = () => pending.close();
    } else {
      pending?.close();
    }
  };
}
