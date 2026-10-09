"use client";

// "Any Questions" (ARCHITECTURE.md §4.3, §11.2), the copilot chat, as the staff sidebar: ask in plain
// words; each tool it calls appears under its answer as it runs, then the answer. POST /api/copilot (SSE).
//
// The chat lives in the browser only: there is no table for it, so a reload starts a new one. Each
// question sends the last few turns along so follow-ups make sense. It can look things up and message
// customers when asked, but it can never mark payments paid, take UTRs, or move stock or jobs: those
// tools are never offered to a model (§4.6), whatever is typed here.

import {
  ArrowUp,
  Bot,
  Check,
  CircleAlert,
  FileDown,
  ListChecks,
  LoaderCircle,
  type LucideIcon,
  Package,
  RotateCcw,
  ShieldCheck,
  SquarePen,
  Ticket,
  X,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Markdown } from "@/components/markdown";
import { printAnswer } from "@/components/print-answer";
import { Button } from "@/components/ui/button";
import { useAnyQuestions } from "@/lib/any-questions";
import { postEvents } from "@/lib/sse";

type ToolCall = { tool: string; ok: boolean; ms: number };
type Turn =
  | { role: "user"; text: string }
  | { role: "assistant"; text: string; tools: ToolCall[]; servers: string[]; state: "running" | "done" | "error"; error?: string; model?: string; finishedAt?: string };

type CopilotEvent =
  | { type: "servers"; servers: string[] }
  | { type: "tool"; tool: string; ok: boolean; ms: number }
  | { type: "delta"; text: string }
  | { type: "done"; model: string; hit_limit: boolean }
  | { type: "error"; message: string };

// Suggested questions, each with what it is about. They stay at the top of the chat.
const EXAMPLES: { text: string; icon: LucideIcon }[] = [
  { text: "Which open tickets are about batteries?", icon: Ticket },
  { text: "How many BAT-AX14 batteries are in stock?", icon: Package },
  { text: "Is serial AX14-7F3K92 still under warranty?", icon: ShieldCheck },
  { text: "What has been tried on SR-2026-00042?", icon: ListChecks },
];

const HISTORY_TURNS = 8;

/** "tickets__search_tickets" -> "Search tickets" with its server. */
function toolLabel(name: string): { server: string; action: string } {
  const [server, tool = name] = name.split("__");
  const action = tool.replace(/_/g, " ");
  return { server, action: action.charAt(0).toUpperCase() + action.slice(1) };
}

export function AnyQuestionsChat() {
  const { setOpen, shortcut } = useAnyQuestions();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [printBlocked, setPrintBlocked] = useState<number | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const busy = turns.at(-1)?.role === "assistant" && (turns.at(-1) as { state: string }).state === "running";

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  function update(fn: (turn: Extract<Turn, { role: "assistant" }>) => Extract<Turn, { role: "assistant" }>) {
    setTurns((current) => {
      const last = current.at(-1);
      if (!last || last.role !== "assistant") return current;
      return [...current.slice(0, -1), fn(last)];
    });
  }

  async function ask(question: string) {
    const text = question.trim();
    if (!text || busy) return;
    // The turns before this one, as the model saw them (answers only once they finished).
    const history = turns
      .filter((t) => t.role === "user" || t.state === "done")
      .slice(-HISTORY_TURNS)
      .map((t) => ({ role: t.role, content: t.text }));
    setDraft("");
    setTurns((current) => [
      ...current,
      { role: "user", text },
      { role: "assistant", text: "", tools: [], servers: [], state: "running" },
    ]);
    try {
      await postEvents<CopilotEvent>("/api/copilot", { message: text, history }, (event) => {
        if (event.type === "servers") update((t) => ({ ...t, servers: event.servers }));
        else if (event.type === "tool") update((t) => ({ ...t, tools: [...t.tools, { tool: event.tool, ok: event.ok, ms: event.ms }] }));
        else if (event.type === "delta") update((t) => ({ ...t, text: t.text + event.text }));
        else if (event.type === "done") update((t) => ({ ...t, state: "done", model: event.model, finishedAt: new Date().toISOString() }));
        else if (event.type === "error") update((t) => ({ ...t, state: "error", error: event.message }));
      });
      // A stream that ended without done or error was cut off.
      update((t) => (t.state === "running" ? { ...t, state: "error", error: "The answer was cut off. Try again." } : t));
    } catch (err: unknown) {
      update((t) => ({ ...t, state: "error", error: err instanceof Error ? err.message : "Any Questions couldn’t answer." }));
    }
  }

  function retry() {
    const lastQuestion = [...turns].reverse().find((t) => t.role === "user");
    if (!lastQuestion) return;
    setTurns((current) => current.slice(0, -2));
    void ask(lastQuestion.text);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex items-start gap-2 px-5 pt-5 pb-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-title-2 font-semibold text-ink">Any Questions</h2>
          <p className="mt-0.5 text-footnote text-ink-secondary">
            Tickets, customers, devices, warranty, prices and stock. It can’t take payments or move stock or jobs.
          </p>
        </div>
        {turns.length ? (
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => {
              setTurns([]);
              input.current?.focus();
            }}
            disabled={busy}
            aria-label="New chat"
            title="New chat"
            className="rounded-full"
          >
            <SquarePen aria-hidden />
          </Button>
        ) : null}
        <Button
          variant="ghost"
          size="icon-sm"
          onClick={() => setOpen(false)}
          aria-label={`Close Any Questions (${shortcut})`}
          title={`Close (${shortcut})`}
          className="rounded-full"
        >
          <X aria-hidden />
        </Button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-5 pb-4 [scrollbar-gutter:stable]">
        <div className="flex flex-col items-start gap-2 pt-1" aria-label="Suggested questions" role="group">
          {EXAMPLES.map(({ text, icon: Icon }) => (
            <button
              key={text}
              type="button"
              onClick={() => void ask(text)}
              disabled={busy}
              className="inline-flex max-w-full items-center gap-2 rounded-full bg-surface px-3.5 py-2 text-left text-[14px] font-medium text-ink ring-1 ring-hairline outline-none transition hover:-translate-y-px hover:ring-violet/50 active:translate-y-0 focus-visible:ring-2 focus-visible:ring-violet disabled:opacity-60"
            >
              <Icon className="size-4 shrink-0 text-violet" aria-hidden />
              <span className="min-w-0 truncate">{text}</span>
            </button>
          ))}
        </div>

        {turns.length === 0 ? (
          <p className="mt-6 text-footnote text-ink-secondary">
            Ask anything about your tickets. This chat isn’t saved: reloading the page starts a new one.
          </p>
        ) : (
          <ol
            className="mt-6 space-y-4 rounded-[24px] bg-canvas/70 p-3 ring-1 ring-hairline dark:bg-surface-raised/40"
            aria-live="polite"
          >
            {turns.map((turn, index) =>
              turn.role === "user" ? (
                <li key={index} className="flex justify-end animate-in fade-in slide-in-from-bottom-1">
                  <p className="max-w-[88%] whitespace-pre-wrap rounded-[16px] bg-accent px-4 py-2.5 text-[15px] font-medium text-on-accent shadow-[0_10px_20px_-14px_color-mix(in_oklab,var(--accent)_90%,transparent)]">
                    {turn.text}
                  </p>
                </li>
              ) : (
                <li key={index} className="flex items-start gap-2.5">
                  <span className="mt-1 grid size-8 shrink-0 place-items-center rounded-full bg-violet/15 text-violet ring-1 ring-violet/25">
                    <Bot className="size-4" aria-hidden />
                  </span>
                  <div className="min-w-0 flex-1 space-y-2.5 rounded-[18px] bg-surface px-4 py-3.5 shadow-card">
                    {turn.tools.length ? (
                      <ul className="flex flex-wrap gap-1.5" aria-label="Tools used">
                        {turn.tools.map((call, i) => {
                          const { server, action } = toolLabel(call.tool);
                          return (
                            <li
                              key={`${call.tool}-${i}`}
                              title={`${server}, ${call.ms} ms`}
                              className="inline-flex items-center gap-1 rounded-full bg-canvas px-2 py-0.5 text-footnote text-ink-secondary animate-in fade-in slide-in-from-left-1 dark:bg-surface-raised"
                            >
                              {call.ok ? (
                                <Check className="size-3 text-success" aria-label="Done" />
                              ) : (
                                <CircleAlert className="size-3 text-danger" aria-label="Refused or failed" />
                              )}
                              {action}
                              <span className="tabular-nums opacity-70">{call.ms} ms</span>
                            </li>
                          );
                        })}
                      </ul>
                    ) : null}
                    {turn.text ? <Markdown text={turn.text} /> : null}
                    {turn.state === "running" ? (
                      <p role="status" className="flex items-center gap-2 text-subheadline text-ink-secondary">
                        <LoaderCircle className="size-4 animate-spin text-violet" aria-hidden />
                        {turn.tools.length ? "Working…" : turn.servers.length ? `Looking in ${turn.servers.join(", ")}…` : "Thinking…"}
                      </p>
                    ) : null}
                    {turn.state === "error" ? (
                      <div role="alert" className="flex flex-wrap items-center gap-2 rounded-control bg-danger/10 px-3 py-2 text-subheadline text-ink">
                        <CircleAlert className="size-4 shrink-0 text-danger" aria-hidden />
                        <span className="min-w-0 flex-1">{turn.error}</span>
                        {index === turns.length - 1 ? (
                          <Button variant="outline" size="sm" onClick={retry}>
                            <RotateCcw aria-hidden /> Try again
                          </Button>
                        ) : null}
                      </div>
                    ) : null}
                    {turn.state === "done" ? (
                      <div className="flex flex-wrap items-center gap-2 border-t border-hairline/70 pt-2">
                        {turn.model ? <p className="min-w-0 truncate text-footnote text-ink-secondary">{turn.model}</p> : null}
                        {turn.text ? (
                          <Button
                            variant="ghost"
                            size="sm"
                            className="ml-auto"
                            onClick={() =>
                              setPrintBlocked(printAnswer(turn.text, turn.finishedAt ?? new Date().toISOString()) ? null : index)
                            }
                          >
                            <FileDown aria-hidden /> Save as PDF
                          </Button>
                        ) : null}
                        {printBlocked === index ? (
                          <p role="alert" className="w-full text-footnote text-danger">
                            The browser blocked the print window. Allow pop-ups for this site and try again.
                          </p>
                        ) : null}
                      </div>
                    ) : null}
                  </div>
                </li>
              ),
            )}
          </ol>
        )}
        <div ref={end} />
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void ask(draft);
        }}
        className="mx-4 mb-1.5 flex items-end gap-2 rounded-[22px] bg-surface p-2 pl-4 ring-[1.5px] ring-hairline transition-shadow focus-within:ring-2 focus-within:ring-accent focus-within:shadow-[0_10px_28px_-14px_color-mix(in_oklab,var(--accent)_60%,transparent)]"
      >
        <label htmlFor="any-questions-input" className="sr-only">
          Ask a question
        </label>
        <textarea
          id="any-questions-input"
          ref={input}
          rows={1}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              void ask(draft);
            }
          }}
          maxLength={2000}
          placeholder="Ask, e.g. which open tickets are about batteries?"
          className="max-h-40 min-h-11 flex-1 resize-none bg-transparent py-3 text-[15px] text-ink outline-none [field-sizing:content] placeholder:text-ink-secondary"
        />
        <Button
          type="submit"
          size="icon"
          disabled={!draft.trim() || busy}
          aria-label="Ask"
          className="size-11 shrink-0 rounded-full transition-transform active:scale-90"
        >
          {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : <ArrowUp className="size-5" aria-hidden />}
        </Button>
      </form>
      <p className="mx-5 mb-3 flex justify-between gap-3 text-[12px] text-ink-secondary">
        <span>Enter to send</span>
        <span>Shift + Enter for a new line</span>
      </p>
    </div>
  );
}
