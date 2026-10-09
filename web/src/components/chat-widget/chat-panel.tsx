"use client";

// The website chat (§6.2, §11.4): a short pre-chat form (name + email, no account), then the conversation
// over WS /ws/chat/{session_id}. Every message runs the customer intake pipeline on the backend, which can
// only reach the catalog, tickets, knowledge and messaging tools (§4.1): never payments, dispatch or stock.
//
// The session id is kept in this browser (localStorage, best effort) so a reload resumes the same
// conversation. A reply that hasn't come 45 s after the server confirmed a message shows the §15 note,
// so the customer is never left looking at silence.

import { CircleAlert, LoaderCircle, Send, Ticket } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { env } from "@/lib/env";

type Line = { id: string; sender: "customer" | "ai" | "note"; text: string; status?: "sending" | "sent" };
type Stored = { sessionId: string; wsPath: string; name: string; greeting: string; ticket?: string };
type ServerEvent =
  | { type: "ready"; session_id: string }
  | { type: "message"; sender: "customer" | "ai"; text: string; at: string }
  | { type: "typing" }
  | { type: "ticket"; ticket_number: string; ticket_id: string }
  | { type: "error"; detail: string };

const STORAGE_KEY = "support-chat-session";
const SILENCE_MS = 45_000;
// §15: what the customer sees if no reply arrives (the backend sends the same words when intake fails).
const SILENCE_NOTE = "We've received your message and an agent will follow up shortly.";
const MAX_RECONNECTS = 5;

function load(): Stored | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Stored) : null;
  } catch {
    return null;
  }
}

function save(value: Stored | null) {
  try {
    if (value) window.localStorage.setItem(STORAGE_KEY, JSON.stringify(value));
    else window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // Private mode or blocked storage: the chat still works, it just won't survive a reload.
  }
}

let counter = 0;
const nextId = () => `l${++counter}`;
const noop = () => () => {};

export function ChatPanel() {
  // The saved session lives in this browser only, so the server renders the loading shell.
  const mounted = useSyncExternalStore(noop, () => true, () => false);
  if (!mounted) {
    return <Shell><p className="p-6 text-subheadline text-ink-secondary">Loading the chat…</p></Shell>;
  }
  return <ChatInBrowser />;
}

function ChatInBrowser() {
  const [session, setSession] = useState<Stored | null>(load);
  return session ? (
    <Conversation
      session={session}
      onTicket={(ticket) => {
        const updated = { ...session, ticket };
        save(updated);
        setSession(updated);
      }}
      onEnd={() => {
        save(null);
        setSession(null);
      }}
    />
  ) : (
    <PreChatForm
      onStarted={(started) => {
        save(started);
        setSession(started);
      }}
    />
  );
}

function Shell({ children, header }: { children: React.ReactNode; header?: React.ReactNode }) {
  return (
    <section
      aria-label="Chat with support"
      className="flex h-[min(640px,calc(100dvh-140px))] min-h-[460px] flex-col overflow-hidden rounded-panel bg-surface shadow-raised"
    >
      <div className="flex items-center gap-3 border-b border-hairline px-4 py-3">
        <span className="size-2 rounded-full bg-success" aria-hidden />
        <p className="flex-1 text-subheadline font-semibold text-ink">Chat with {env.companyName} support</p>
        {header}
      </div>
      {children}
    </section>
  );
}

function PreChatForm({ onStarted }: { onStarted: (session: Stored) => void }) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = String(form.get("name")).trim();
    setPending(true);
    setError(null);
    try {
      const res = await fetch(`${env.apiUrl}/api/chat/session`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email: String(form.get("email")).trim() }),
      });
      if (!res.ok) {
        setError(res.status === 422 ? "Check your name and email address." : "We couldn't start the chat. Try again.");
        setPending(false);
        return;
      }
      const body = (await res.json()) as { session_id: string; ws_path: string; greeting: string };
      onStarted({ sessionId: body.session_id, wsPath: body.ws_path, name, greeting: body.greeting });
    } catch {
      setError("We couldn't reach support. Check your connection and try again.");
      setPending(false);
    }
  }

  return (
    <Shell>
      <form onSubmit={onSubmit} className="flex flex-1 flex-col justify-center gap-5 p-6">
        <div className="space-y-1">
          <h2 className="text-title-2 font-semibold tracking-tight text-ink">Start a chat</h2>
          <p className="text-subheadline text-ink-secondary">
            No account needed. We use your email to link your tickets and send updates.
          </p>
        </div>
        <div className="space-y-2">
          <Label htmlFor="chat-name" className="text-subheadline">Your name</Label>
          <Input id="chat-name" name="name" required maxLength={120} autoComplete="name" className="h-11 rounded-control text-body" />
        </div>
        <div className="space-y-2">
          <Label htmlFor="chat-email" className="text-subheadline">Email</Label>
          <Input id="chat-email" name="email" type="email" required autoComplete="email" className="h-11 rounded-control text-body" />
        </div>
        {error ? <p role="alert" className="text-subheadline text-danger">{error}</p> : null}
        <Button type="submit" disabled={pending} className="h-11 rounded-control text-[17px]">
          {pending ? "Starting…" : "Start chat"}
        </Button>
      </form>
    </Shell>
  );
}

function Conversation({ session, onTicket, onEnd }: {
  session: Stored; onTicket: (ticket: string) => void; onEnd: () => void;
}) {
  const [lines, setLines] = useState<Line[]>([{ id: "greeting", sender: "ai", text: session.greeting }]);
  const [state, setState] = useState<"connecting" | "open" | "reconnecting" | "lost">("connecting");
  const [typing, setTyping] = useState(false);
  const [draft, setDraft] = useState("");
  const socket = useRef<WebSocket | null>(null);
  const silence = useRef<ReturnType<typeof setTimeout> | null>(null);
  const end = useRef<HTMLDivElement>(null);
  // The callbacks change on every parent render; the socket must not reconnect because of that.
  const handlers = useRef({ onTicket, onEnd });
  useEffect(() => {
    handlers.current = { onTicket, onEnd };
  }, [onTicket, onEnd]);

  const clearSilence = () => {
    if (silence.current) clearTimeout(silence.current);
    silence.current = null;
  };

  const reconnect = useRef<(attempt: number) => void>(() => {});
  const connect = useCallback((attempt: number) => {
    const ws = new WebSocket(`${env.wsUrl}${session.wsPath}`);
    socket.current = ws;
    let opened = false;
    ws.onopen = () => {
      opened = true;
      setState("open");
    };
    ws.onmessage = (raw) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(String(raw.data)) as ServerEvent;
      } catch {
        return;
      }
      if (event.type === "typing") setTyping(true);
      else if (event.type === "ticket") handlers.current.onTicket(event.ticket_number);
      else if (event.type === "error") setLines((ls) => [...ls, { id: nextId(), sender: "note", text: event.detail }]);
      else if (event.type === "message" && event.sender === "customer") {
        // The server's echo confirms our message: mark the oldest one still sending as sent.
        setLines((ls) => {
          const i = ls.findIndex((l) => l.sender === "customer" && l.status === "sending" && l.text === event.text);
          if (i === -1) return [...ls, { id: nextId(), sender: "customer", text: event.text, status: "sent" }];
          return ls.map((l, j) => (j === i ? { ...l, status: "sent" } : l));
        });
        clearSilence();
        silence.current = setTimeout(() => {
          setTyping(false);
          setLines((ls) => [...ls, { id: nextId(), sender: "note", text: SILENCE_NOTE }]);
        }, SILENCE_MS);
      } else if (event.type === "message") {
        clearSilence();
        setTyping(false);
        setLines((ls) => [...ls, { id: nextId(), sender: "ai", text: event.text }]);
      }
    };
    ws.onclose = (close) => {
      if (socket.current !== ws) return; // replaced or unmounted
      if (close.code === 1008) {
        handlers.current.onEnd(); // the server doesn't know this session any more: start over
        return;
      }
      if (attempt < MAX_RECONNECTS) {
        setState("reconnecting");
        setTimeout(() => reconnect.current(opened ? 0 : attempt + 1), Math.min(1000 * 2 ** attempt, 8000));
      } else {
        setState("lost");
      }
    };
  }, [session.wsPath]);

  useEffect(() => {
    reconnect.current = connect;
    connect(0);
    return () => {
      const ws = socket.current;
      socket.current = null;
      ws?.close();
      clearSilence();
    };
  }, [connect]);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [lines, typing]);

  function send(event?: { preventDefault(): void }) {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || state !== "open" || !socket.current) return;
    socket.current.send(JSON.stringify({ text }));
    setLines((ls) => [...ls, { id: nextId(), sender: "customer", text, status: "sending" }]);
    setDraft("");
  }

  return (
    <Shell
      header={
        <button type="button" onClick={onEnd} className="rounded-control text-footnote text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent">
          New chat
        </button>
      }
    >
      {session.ticket ? (
        <div className="flex items-center gap-2 bg-accent/8 px-4 py-2 text-footnote text-ink">
          <Ticket className="size-4 text-accent" aria-hidden />
          Your ticket <span className="font-semibold tabular-nums">{session.ticket}</span>
        </div>
      ) : null}
      <ol className="flex-1 space-y-2.5 overflow-y-auto px-4 py-4" aria-live="polite">
        {lines.map((line) =>
          line.sender === "note" ? (
            <li key={line.id} className="flex items-start gap-2 rounded-control bg-warning/10 px-3 py-2 text-footnote text-ink">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-warning" aria-hidden /> {line.text}
            </li>
          ) : (
            <li key={line.id} className={`flex ${line.sender === "customer" ? "justify-end" : "justify-start"}`}>
              <div className="max-w-[85%] space-y-0.5">
                <p className={`whitespace-pre-wrap rounded-[18px] px-3.5 py-2 text-subheadline ${
                  line.sender === "customer" ? "bg-accent text-on-accent" : "bg-canvas text-ink dark:bg-surface-raised"
                }`}>
                  {line.text}
                </p>
                {line.sender === "customer" ? (
                  <p className="pr-1 text-right text-[11px] text-ink-secondary">{line.status === "sending" ? "Sending…" : "Delivered"}</p>
                ) : null}
              </div>
            </li>
          ),
        )}
        {typing ? (
          <li className="flex items-center gap-2 text-footnote text-ink-secondary" role="status">
            <LoaderCircle className="size-3.5 animate-spin" aria-hidden /> Support is typing…
          </li>
        ) : null}
        <div ref={end} />
      </ol>
      {state !== "open" ? (
        <p role="status" className="border-t border-hairline px-4 py-2 text-footnote text-ink-secondary">
          {state === "connecting" ? "Connecting…" : state === "reconnecting" ? "Reconnecting…" : (
            <>The connection was lost. <button type="button" className="text-accent hover:underline" onClick={() => { setState("reconnecting"); connect(0); }}>Try again</button></>
          )}
        </p>
      ) : null}
      <form onSubmit={send} className="flex items-end gap-2 border-t border-hairline p-3">
        <label htmlFor="chat-input" className="sr-only">Your message</label>
        <textarea
          id="chat-input"
          rows={1}
          value={draft}
          maxLength={4000}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) send(event);
          }}
          placeholder="Describe the problem, and your serial number if you have it"
          className="max-h-32 min-h-10 flex-1 resize-none rounded-control bg-canvas px-3 py-2 text-subheadline text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised"
        />
        <Button type="submit" disabled={!draft.trim() || state !== "open"} className="h-10 rounded-control text-[15px]" aria-label="Send">
          <Send aria-hidden />
        </Button>
      </form>
    </Shell>
  );
}
