"use client";

import { AlertCircle, ArrowUp, Check, CheckCheck, LoaderCircle, RotateCcw, Ticket } from "lucide-react";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useSyncExternalStore,
  type FormEvent,
  type KeyboardEvent,
} from "react";

import { BrandMark, COMPANY_NAME } from "@/components/brand";
import {
  clearChat,
  loadChat,
  saveChat,
  useChat,
  type ChatMessage,
  type ChatSession,
} from "@/components/chat-widget/use-chat";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { cn } from "@/lib/utils";

const MAX_TEXT = 4000;
const noopSubscribe = () => () => {};

/** The website chat (§11.4): pre-chat form, then the conversation. */
export function ChatPanel({ className }: { className?: string }) {
  // The stored session is only known in the browser.
  const hydrated = useSyncExternalStore(noopSubscribe, () => true, () => false);
  const [session, setSession] = useState<ChatSession | null | undefined>(undefined);
  const [ended, setEnded] = useState(false);
  // Read once: a new object each render would reconnect the socket each render.
  const stored = useMemo(() => (hydrated ? loadChat() : null), [hydrated]);
  const current = session === undefined ? stored : session;

  const onGone = useCallback(() => {
    clearChat();
    setEnded(true);
    setSession(null);
  }, []);

  function start(next: ChatSession) {
    saveChat(next);
    setEnded(false);
    setSession(next);
  }

  function newChat() {
    clearChat();
    setEnded(false);
    setSession(null);
  }

  return (
    <section
      aria-label="Chat with support"
      className={cn("flex min-h-0 flex-col overflow-hidden rounded-panel bg-surface", className)}
    >
      {!hydrated ? null : current ? (
        <Conversation session={current} onGone={onGone} onNewChat={newChat} />
      ) : (
        <PreChat onStart={start} ended={ended} />
      )}
    </section>
  );
}

// ---------- pre-chat form ----------

function PreChat({ onStart, ended }: { onStart: (s: ChatSession) => void; ended: boolean }) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const opened = await api<ChatSession>("/api/chat/session", {
        method: "POST",
        body: { name: name.trim(), email: email.trim() },
        auth: false,
      });
      onStart(opened);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 422
          ? "Check your name and email address."
          : caught instanceof ApiError
            ? caught.message
            : "Something went wrong. Try again.",
      );
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-1 flex-col justify-center px-5 py-8 sm:px-8">
      <BrandMark className="size-12" />
      <h2 className="mt-4 text-title-2 font-semibold">Chat with support</h2>
      <p className="mt-1 text-subheadline text-pretty text-ink-secondary">
        {ended
          ? "That chat has ended. Start a new one and we'll pick up from here."
          : "Tell us who you are and what's wrong. No account needed."}
      </p>
      <form onSubmit={submit} className="mt-6 space-y-4" noValidate>
        <div>
          <Label htmlFor="chat-name">Name</Label>
          <Input id="chat-name" autoComplete="name" required maxLength={80} value={name} onChange={(e) => setName(e.target.value)} />
        </div>
        <div>
          <Label htmlFor="chat-email">Email</Label>
          <Input
            id="chat-email"
            type="email"
            inputMode="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="name@example.com"
          />
          <p className="mt-1.5 text-footnote text-ink-secondary">So we can link your devices and reach you about this ticket.</p>
        </div>
        <div aria-live="polite">
          {error && (
            <p className="flex items-start gap-2 text-subheadline text-danger">
              <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              {error}
            </p>
          )}
        </div>
        <Button type="submit" className="w-full" disabled={busy || !name.trim() || !email.trim()}>
          {busy && <LoaderCircle aria-hidden="true" className="animate-spin" />}
          {busy ? "Starting" : "Start chat"}
        </Button>
      </form>
    </div>
  );
}

// ---------- the conversation ----------

function Conversation({ session, onGone, onNewChat }: { session: ChatSession; onGone: () => void; onNewChat: () => void }) {
  const { messages, typing, ticketNumber, connection, notice, send } = useChat(session, onGone);
  const [text, setText] = useState("");
  const log = useRef<HTMLOListElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  // Keep the newest message in view.
  useLayoutEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, typing]);

  // The textarea grows with its text, up to about five lines.
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [text]);

  function submit(event?: FormEvent) {
    event?.preventDefault();
    const body = text.trim();
    if (!body || body.length > MAX_TEXT) return;
    send(body);
    setText("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
    // With a draft, Escape keeps the page (Escape otherwise goes home, §11.2).
    if (event.key === "Escape" && text) event.preventDefault();
  }

  const lastCustomer = [...messages].reverse().find((m) => m.sender === "customer" && !m.local);

  return (
    <>
      <header className="flex items-center gap-3 border-b border-hairline px-4 py-3 sm:px-5">
        <BrandMark className="size-9" />
        <div className="min-w-0 flex-1">
          <p className="truncate font-semibold">{COMPANY_NAME} support</p>
          <p className="flex items-center gap-1.5 text-footnote text-ink-secondary" aria-live="polite">
            <span
              aria-hidden="true"
              className={cn("size-1.5 rounded-full", connection === "open" ? "bg-success" : "bg-warning")}
            />
            {connection === "open" ? "Online" : connection === "connecting" ? "Connecting" : "Reconnecting"}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onNewChat} aria-label="New chat" title="New chat">
          <RotateCcw aria-hidden="true" />
          <span className="hidden sm:inline">New chat</span>
        </Button>
      </header>

      {ticketNumber && (
        <p className="flex items-center gap-2 border-b border-hairline bg-accent/5 px-4 py-2.5 text-subheadline sm:px-5">
          <Ticket aria-hidden="true" className="size-4 shrink-0 text-accent" />
          <span>
            Ticket <span className="font-semibold tabular-nums">{ticketNumber}</span>
            <span className="text-ink-secondary"> · we&apos;ll keep you posted here</span>
          </span>
        </p>
      )}

      <ol ref={log} role="log" aria-label="Messages" aria-live="polite" className="min-h-0 flex-1 space-y-2 overflow-y-auto px-4 py-4 sm:px-5">
        {messages.length === 0 && (
          <li className="py-8 text-center text-subheadline text-pretty text-ink-secondary">
            Hi {session.name.split(" ")[0]}. What&apos;s wrong with your device? If you have the serial number handy, include it.
          </li>
        )}
        {messages.map((message) => (
          <Bubble key={message.key} message={message} showStatus={message === lastCustomer} />
        ))}
        {typing && (
          <li aria-label="Support is typing" className="flex">
            <span className="inline-flex items-center gap-1 rounded-panel rounded-bl-control bg-canvas px-4 py-3.5">
              {[0, 150, 300].map((delay) => (
                <span
                  key={delay}
                  className="size-1.5 animate-bounce rounded-full bg-ink-secondary"
                  style={{ animationDelay: `${delay}ms` }}
                />
              ))}
            </span>
          </li>
        )}
      </ol>

      {notice && <p className="px-5 pb-1 text-footnote text-danger">{notice}</p>}

      <form onSubmit={submit} className="flex items-end gap-2 border-t border-hairline p-3">
        <label htmlFor="chat-text" className="sr-only">Message</label>
        <textarea
          id="chat-text"
          ref={box}
          rows={1}
          value={text}
          maxLength={MAX_TEXT}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Message"
          className="min-h-11 w-full min-w-0 flex-1 resize-none rounded-panel bg-canvas px-4 py-2.5 text-body text-ink placeholder:text-ink-secondary focus:ring-2 focus:ring-accent focus:outline-none focus-visible:outline-none"
        />
        <Button type="submit" size="icon" aria-label="Send" disabled={!text.trim()} className="size-11">
          <ArrowUp aria-hidden="true" className="size-5!" />
        </Button>
      </form>
    </>
  );
}

function Bubble({ message, showStatus }: { message: ChatMessage; showStatus: boolean }) {
  const mine = message.sender === "customer";
  return (
    <li className={cn("flex flex-col", mine ? "items-end" : "items-start")}>
      <p
        className={cn(
          "max-w-[85%] rounded-panel px-4 py-2.5 text-subheadline whitespace-pre-wrap text-pretty",
          mine ? "rounded-br-control bg-accent text-on-accent" : "rounded-bl-control bg-canvas text-ink",
          message.status === "sending" && "opacity-70",
        )}
      >
        {message.text}
      </p>
      {mine && message.status === "failed" && (
        <span className="mt-1 flex items-center gap-1 px-1 text-footnote text-danger">
          <AlertCircle aria-hidden="true" className="size-3.5" />
          Not sent: {message.error}
        </span>
      )}
      {mine && showStatus && message.status !== "failed" && (
        <span className="mt-1 flex items-center gap-1 px-1 text-footnote text-ink-secondary">
          {message.status === "sending" ? (
            <>
              <Check aria-hidden="true" className="size-3.5" />
              Sending
            </>
          ) : (
            <>
              <CheckCheck aria-hidden="true" className="size-3.5 text-accent" />
              Delivered
            </>
          )}
        </span>
      )}
    </li>
  );
}
