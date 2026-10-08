"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { WS_URL } from "@/lib/api";

/**
 * The website chat's socket (§11.4), WS /ws/chat/{session_id}. Frames are documented in
 * backend/app/channels/web_chat.py.
 *
 * A dropped socket reconnects with backoff; 1008 means the server no longer knows the session,
 * so the page starts a new chat. A message is "sending" until the server echoes it with its
 * client_id ("delivered"); one that never got its echo is sent again with the same client_id
 * after a reconnect (the server stores it once).
 */

/** The same words as the backend's FALLBACK_REPLY_NO_TICKET (app/channels/inbound.py). */
export const FALLBACK_REPLY_NO_TICKET = "Thanks, we've received your message. An agent will follow up shortly.";
const FALLBACK_AFTER_MS = 45_000;
const TYPING_TIMEOUT_MS = 20_000;
const STORE = "servicemesh.chat";

export type ChatSession = { session_id: string; name: string };

export type ChatMessage = {
  key: string;
  id: string | null;
  sender: "customer" | "support";
  text: string;
  createdAt: string;
  clientId: string | null;
  status?: "sending" | "delivered" | "failed";
  error?: string;
  /** Shown by the page itself (the 45 s fallback), not sent by the server. */
  local?: boolean;
};

export type Connection = "connecting" | "open" | "reconnecting";

type ServerMessage = {
  id: string | null;
  sender: "customer" | "support";
  text: string;
  created_at: string;
  client_id: string | null;
};

type Frame =
  | { type: "session"; session_id: string; name: string | null; ticket_number: string | null; messages: ServerMessage[] }
  | ({ type: "message" } & ServerMessage)
  | { type: "typing"; on: boolean }
  | { type: "ticket"; ticket_number: string }
  | { type: "error"; detail: string; client_id?: string | null };

// ---------- the session id, kept in the browser (best effort) ----------

export function loadChat(): ChatSession | null {
  try {
    const raw = window.localStorage.getItem(STORE);
    const parsed = raw ? (JSON.parse(raw) as ChatSession) : null;
    return parsed?.session_id ? parsed : null;
  } catch {
    return null;
  }
}

export function saveChat(session: ChatSession): void {
  try {
    window.localStorage.setItem(STORE, JSON.stringify(session));
  } catch {}
}

export function clearChat(): void {
  try {
    window.localStorage.removeItem(STORE);
  } catch {}
}

function newClientId(): string {
  // crypto.randomUUID needs a secure context; a phone on a LAN address may not have one.
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function fromServer(m: ServerMessage): ChatMessage {
  return {
    key: m.id ?? m.client_id ?? newClientId(),
    id: m.id,
    sender: m.sender,
    text: m.text,
    createdAt: m.created_at,
    clientId: m.client_id,
    status: m.sender === "customer" ? "delivered" : undefined,
  };
}

// ---------- the hook ----------

export function useChat(session: ChatSession | null, onGone: () => void) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [typing, setTyping] = useState(false);
  const [ticketNumber, setTicketNumber] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [notice, setNotice] = useState<string | null>(null);

  const socket = useRef<WebSocket | null>(null);
  const unconfirmed = useRef(new Map<string, string>()); // client_id → text, until the echo
  const fallbackTimer = useRef<number | undefined>(undefined);
  const typingTimer = useRef<number | undefined>(undefined);
  const onGoneRef = useRef(onGone);
  useEffect(() => {
    onGoneRef.current = onGone;
  }, [onGone]);

  const sendFrame = useCallback((clientId: string, text: string) => {
    const ws = socket.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "message", text, client_id: clientId }));
  }, []);

  useEffect(() => {
    if (!session) return;
    const pending = unconfirmed.current;
    let attempt = 0;
    let retry: number | undefined;
    let stopped = false;

    function stopFallback() {
      window.clearTimeout(fallbackTimer.current);
      fallbackTimer.current = undefined;
    }

    function startFallback() {
      stopFallback();
      // Never silence (§15): if no reply has come 45 s after the server confirmed a message.
      fallbackTimer.current = window.setTimeout(() => {
        fallbackTimer.current = undefined;
        setTyping(false);
        setMessages((current) => [
          ...current,
          { key: `fallback-${Date.now()}`, id: null, sender: "support", text: FALLBACK_REPLY_NO_TICKET,
            createdAt: new Date().toISOString(), clientId: null, local: true },
        ]);
      }, FALLBACK_AFTER_MS);
    }

    function handle(frame: Frame) {
      switch (frame.type) {
        case "session": {
          attempt = 0;
          setConnection("open");
          setNotice(null);
          setTicketNumber(frame.ticket_number);
          const history = frame.messages.map(fromServer);
          const known = new Set(history.map((m) => m.clientId).filter(Boolean));
          for (const clientId of known) pending.delete(clientId as string);
          setMessages((current) => [
            ...history,
            // Still waiting for their echo: keep them, and send them again below.
            ...current.filter((m) => m.status === "sending" && m.clientId && !known.has(m.clientId)),
          ]);
          for (const [clientId, text] of pending) sendFrame(clientId, text);
          break;
        }
        case "message": {
          const incoming = fromServer(frame);
          if (frame.sender === "customer") {
            if (frame.client_id) pending.delete(frame.client_id);
            setMessages((current) =>
              current.some((m) => m.clientId && m.clientId === frame.client_id)
                ? current.map((m) =>
                    m.clientId === frame.client_id ? { ...m, status: "delivered", createdAt: frame.created_at } : m,
                  )
                : [...current, incoming], // sent from another tab
            );
            startFallback();
          } else {
            stopFallback();
            setTyping(false);
            setMessages((current) => (incoming.id && current.some((m) => m.id === incoming.id) ? current : [...current, incoming]));
          }
          break;
        }
        case "typing":
          window.clearTimeout(typingTimer.current);
          setTyping(frame.on);
          if (frame.on) typingTimer.current = window.setTimeout(() => setTyping(false), TYPING_TIMEOUT_MS);
          break;
        case "ticket":
          setTicketNumber(frame.ticket_number);
          break;
        case "error":
          if (frame.client_id) {
            pending.delete(frame.client_id);
            setMessages((current) =>
              current.map((m) => (m.clientId === frame.client_id ? { ...m, status: "failed", error: frame.detail } : m)),
            );
          } else {
            setNotice(frame.detail);
          }
          break;
      }
    }

    function connect() {
      window.clearTimeout(retry);
      setConnection(attempt === 0 ? "connecting" : "reconnecting");
      const ws = new WebSocket(`${WS_URL}/ws/chat/${encodeURIComponent(session!.session_id)}`);
      socket.current = ws;
      ws.onmessage = (event) => {
        try {
          handle(JSON.parse(event.data) as Frame);
        } catch {
          // a frame we can't read is skipped
        }
      };
      ws.onclose = (event) => {
        if (socket.current === ws) socket.current = null;
        if (stopped) return;
        if (event.code === 1008) {
          onGoneRef.current();
          return;
        }
        // 1, 2, 4 … 30 s, with jitter so a server restart isn't met by every browser at once.
        const delay = Math.min(30_000, 1000 * 2 ** attempt) * (0.75 + Math.random() * 0.5);
        attempt += 1;
        setConnection("reconnecting");
        retry = window.setTimeout(connect, delay);
      };
    }

    function reconnectNow() {
      if (!socket.current) {
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
      stopFallback();
      window.clearTimeout(typingTimer.current);
      const ws = socket.current;
      socket.current = null;
      if (ws) ws.onmessage = null;
      // Closing a socket that is still connecting logs a browser warning: close it once open.
      if (ws?.readyState === WebSocket.CONNECTING) ws.onopen = () => ws.close(1000);
      else ws?.close(1000);
      pending.clear();
      setMessages([]);
      setTicketNumber(null);
      setTyping(false);
    };
  }, [session, sendFrame]);

  const send = useCallback(
    (text: string) => {
      const clientId = newClientId();
      unconfirmed.current.set(clientId, text);
      setMessages((current) => [
        ...current,
        { key: clientId, id: null, sender: "customer", text, createdAt: new Date().toISOString(), clientId, status: "sending" },
      ]);
      sendFrame(clientId, text); // if the socket is down, it goes when it reconnects
    },
    [sendFrame],
  );

  return { messages, typing, ticketNumber, connection, notice, send };
}
