"use client";

// The reply composer (ARCHITECTURE.md sections 7.3 and 7.5).
//
//   write a note -> Polish -> original and polished side by side -> edit -> Send
//   type "/"     -> the command menu -> /payments [words] -> Enter -> its steps, streamed inline
//
// Nothing polished leaves without the agent seeing it: Send in the preview is the only path that
// posts polished text, and it posts what is in the editable box, not what the model returned.
// Slash commands are for agents and admins (the backend checks too). The menu lists what
// GET /api/commands returns: the built-ins and the agent's own custom commands.

import { LoaderCircle, Play, Send, Sparkles, StickyNote, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { CommandMenu, type CommandRun, CommandRunPanel } from "@/components/composer/slash-commands";
import { type Chip, SuggestionChips } from "@/components/composer/suggestion-chips";
import { Button } from "@/components/ui/button";
import { type PolishResult, type SendMessageResult, type SourceChannel, ApiError, api } from "@/lib/api";
import { type Command as SlashCommand, hasNeededWords, parseCommand, runCommand } from "@/lib/commands";
import { CHANNEL_LABEL } from "@/lib/format";
import { useStaffUser } from "@/lib/session";

type Preview = { note: string; result: PolishResult };
type Notice = { tone: "ok" | "error"; text: string };

const FIELD =
  "w-full resize-y rounded-control bg-canvas px-3 py-2 text-subheadline text-ink placeholder:text-ink-secondary " +
  "outline-none focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised";

export function Composer({
  ticketId,
  replyChannel,
  onSent,
  suggestionsKey,
}: {
  ticketId: string;
  /** Changes when the ticket gets a new message or event, so the suggested chips are fetched again. */
  suggestionsKey: number;
  /** The channel the customer last wrote or was answered on: where the backend sends the reply. */
  replyChannel: SourceChannel | null;
  onSent: () => void;
}) {
  const [text, setText] = useState("");
  const [internal, setInternal] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  // What will be sent from the preview: the model's rewrite, as the agent has edited it.
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState<"polish" | "send" | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const role = useStaffUser()?.role;
  const staff = role === "agent" || role === "admin";
  const [commandList, setCommandList] = useState<SlashCommand[] | null>(null);
  const [commandsError, setCommandsError] = useState<string | null>(null);
  const [menuValue, setMenuValue] = useState("");
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [run, setRun] = useState<CommandRun | null>(null);
  // Bumped after a command runs here, since it can change which chips make sense.
  const [ranCommands, setRanCommands] = useState(0);

  useEffect(() => {
    if (!staff) return;
    let cancelled = false;
    api
      .commands()
      .then((body) => {
        if (!cancelled) setCommandList(body.commands);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setCommandsError(message(err, "Couldn’t load the commands."));
          setCommandList([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [staff]);

  // A "/" at the start is a command for agents and admins, never in an internal note.
  const command = !internal && staff ? parseCommand(text) : null;
  const naming = command !== null && !/\s/.test(text.trimStart());  // still typing the name
  const matches = command && commandList ? commandList.filter((c) => c.name.startsWith(command.name)) : [];
  const ready = command && !naming ? commandList?.find((c) => c.name === command.name) ?? null : null;
  const runnable = ready !== null && command !== null && hasNeededWords(ready, command.args);
  const menuOpen = naming && !menuDismissed && run?.state !== "running";
  const running = run?.state === "running";

  const canPolish = text.trim().length > 0 && !internal && busy === null && command === null;

  function choose(chosen: SlashCommand) {
    setText(`/${chosen.name} `);
    setMenuDismissed(false);
    requestAnimationFrame(() => textarea.current?.focus());
  }

  function runReady() {
    if (!ready || !command || !runnable) return;
    void start(ready.name, command.args, true);
  }

  /** Run a command and stream its steps under the composer. `typed`: it came from the text box. */
  async function start(name: string, args: string, typed: boolean) {
    if (running || busy !== null) return;
    setNotice(null);
    setRun({ command: name, args, state: "running", events: [] });
    let failed = false;
    try {
      await runCommand(ticketId, name, args, (event) => {
        if (event.type === "error") failed = true;
        setRun((current) => (current ? { ...current, events: [...current.events, event] } : current));
      });
      setRun((current) => (current ? { ...current, state: failed ? "error" : "done" } : current));
      if (!failed && typed) setText("");
      setRanCommands((n) => n + 1);
    } catch (err: unknown) {
      setRun((current) =>
        current
          ? { ...current, state: "error", events: [...current.events, { type: "error", message: message(err, "The command failed.") }] }
          : current,
      );
    } finally {
      onSent();
    }
  }

  function onComposerKeyDown(event: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (menuOpen && matches.length) {
      const index = Math.max(0, matches.findIndex((c) => c.name === menuValue));
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const next = (index + (event.key === "ArrowDown" ? 1 : matches.length - 1)) % matches.length;
        setMenuValue(matches[next].name);
        return;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        event.preventDefault();
        choose(matches[index]);
        return;
      }
    }
    if (menuOpen && event.key === "Escape") {
      event.preventDefault();
      setMenuDismissed(true);
      return;
    }
    if (ready && event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      runReady();
      return;
    }
    onKeyDown(event, plainSend);
  }

  async function polish() {
    if (!canPolish) return;
    setBusy("polish");
    setNotice(null);
    try {
      const result = await api.polish(ticketId, text.trim());
      setPreview({ note: text.trim(), result });
      setDraft(result.polished);
    } catch (err: unknown) {
      setNotice({ tone: "error", text: message(err, "Couldn't polish that. Your note is untouched.") });
    } finally {
      setBusy(null);
    }
  }

  async function send(body: { text: string; original?: string }) {
    if (!body.text.trim() || busy !== null) return;
    setBusy("send");
    setNotice(null);
    try {
      const result = await api.sendMessage(ticketId, { ...body, internal_note: internal });
      setNotice(describeSend(result, internal));
      setText("");
      setPreview(null);
      setDraft("");
      onSent();
    } catch (err: unknown) {
      // The draft stays exactly where it was, so a failed send loses nothing.
      setNotice({ tone: "error", text: message(err, "Couldn't send that.") });
    } finally {
      setBusy(null);
    }
  }

  const plainSend = () => void send({ text: text.trim() });

  function pick(chip: Chip) {
    if (chip.needs_args) {
      // /escalate and /close need the agent's own words: fill the box and let them finish it.
      setInternal(false);
      setText(`/${chip.name} `);
      requestAnimationFrame(() => {
        textarea.current?.focus();
        textarea.current?.setSelectionRange(chip.name.length + 2, chip.name.length + 2);
      });
      return;
    }
    void start(chip.name, chip.args, false);
  }

  function backToEditing() {
    setPreview(null);
    setDraft("");
    requestAnimationFrame(() => textarea.current?.focus());
  }

  function onKeyDown(event: React.KeyboardEvent, action: () => void) {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      action();
    }
  }

  if (preview) {
    const { result } = preview;
    const sendPolished = () => void send({ text: draft.trim(), original: preview.note });
    return (
      <section aria-label="Polish preview" className="space-y-3 rounded-card bg-surface p-4 animate-in fade-in slide-in-from-bottom-1">
        {result.warning ? (
          <div role="alert" className="flex items-start gap-2 rounded-control bg-warning/15 px-3 py-2 text-subheadline text-ink">
            <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
            <p>
              <span className="font-medium">Review the rewrite. </span>
              {result.warning}
            </p>
          </div>
        ) : null}
        {!result.was_polished ? (
          <p role="status" className="rounded-control bg-canvas px-3 py-2 text-subheadline text-ink-secondary dark:bg-surface-raised">
            {result.reason ?? "This wasn’t polished."} You can send your own note as written.
          </p>
        ) : null}

        <div className="grid gap-3 md:grid-cols-2">
          <div className="space-y-1.5">
            <label htmlFor="polish-original" className="text-footnote font-medium text-ink-secondary">
              Your note
            </label>
            <textarea id="polish-original" readOnly value={preview.note} rows={5} className={`${FIELD} text-ink-secondary`} />
          </div>
          <div className="space-y-1.5">
            <label htmlFor="polish-draft" className="text-footnote font-medium text-ink-secondary">
              {result.was_polished ? "Polished reply, editable" : "Reply to send, editable"}
            </label>
            <textarea
              id="polish-draft"
              autoFocus
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => onKeyDown(event, sendPolished)}
              rows={5}
              className={FIELD}
            />
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Button variant="ghost" onClick={backToEditing} disabled={busy !== null} className="rounded-control">
            Back to editing
          </Button>
          <span className="ml-auto text-footnote text-ink-secondary">
            {replyChannel ? `Sends on ${CHANNEL_LABEL[replyChannel]}` : null}
          </span>
          <Button
            variant="outline"
            onClick={() => void send({ text: preview.note })}
            disabled={busy !== null}
            className="rounded-control"
          >
            Send my note instead
          </Button>
          <Button onClick={sendPolished} disabled={busy !== null || !draft.trim()} className="rounded-control">
            {busy === "send" ? <LoaderCircle className="animate-spin" aria-hidden /> : <Send aria-hidden />}
            {result.was_polished ? "Send polished reply" : "Send"}
          </Button>
        </div>
        <Status notice={notice} />
      </section>
    );
  }

  return (
    <section
      aria-label="Reply"
      className={`space-y-2 rounded-card p-3 ${internal ? "bg-warning/10" : "bg-surface"}`}
    >
      {staff && !internal ? (
        <SuggestionChips
          ticketId={ticketId}
          refreshKey={`${suggestionsKey}:${ranCommands}`}
          disabled={running || busy !== null}
          onPick={pick}
        />
      ) : null}
      {run ? <CommandRunPanel run={run} onDismiss={() => setRun(null)} /> : null}
      {menuOpen && command ? (
        <CommandMenu
          query={command.name}
          matches={matches}
          value={menuValue}
          onValueChange={setMenuValue}
          onChoose={choose}
          loadError={commandsError}
          loading={commandList === null}
        />
      ) : null}
      <label htmlFor="composer-text" className="sr-only">
        {internal ? "Internal note" : "Reply to the customer, or type / for commands"}
      </label>
      <textarea
        id="composer-text"
        ref={textarea}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          setMenuDismissed(false);
        }}
        onKeyDown={onComposerKeyDown}
        rows={3}
        placeholder={
          internal
            ? "Write a note for your team. It is never sent."
            : role === "agent" || role === "admin"
              ? "Reply, or type / for commands"
              : "Write a reply to the customer"
        }
        className={FIELD}
      />
      {ready ? (
        <p className="text-footnote text-ink-secondary">
          {!runnable
            ? `/${ready.name} needs words after it: ${ready.usage}${ready.example ? `, e.g. /${ready.name} ${ready.example}` : ""}.`
            : command?.args
              ? `Enter runs /${ready.name} with “${command.args}”.`
              : `Enter runs /${ready.name}: ${ready.description}.${ready.example ? ` You can add words, e.g. /${ready.name} ${ready.example}.` : ""}`}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          role="switch"
          aria-checked={internal}
          onClick={() => setInternal((current) => !current)}
          className="inline-flex items-center gap-2 rounded-control px-2 py-1.5 text-subheadline text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent"
        >
          <span
            aria-hidden
            className={`relative h-5 w-9 rounded-full transition-colors ${internal ? "bg-warning" : "bg-hairline"}`}
          >
            <span
              className={`absolute top-0.5 size-4 rounded-full bg-surface transition-[left] ${internal ? "left-[18px]" : "left-0.5"}`}
            />
          </span>
          <StickyNote className="size-4" aria-hidden />
          Internal note
        </button>
        <span className="ml-auto text-footnote text-ink-secondary">
          {internal ? "Stays on the ticket" : replyChannel ? `Sends on ${CHANNEL_LABEL[replyChannel]}` : null}
        </span>
        {!internal && !ready ? (
          <Button variant="outline" onClick={() => void polish()} disabled={!canPolish} className="rounded-control">
            {busy === "polish" ? <LoaderCircle className="animate-spin" aria-hidden /> : <Sparkles aria-hidden />}
            Polish
          </Button>
        ) : null}
        {ready ? (
          <Button onClick={runReady} disabled={!runnable || running || busy !== null} className="rounded-control">
            {running ? <LoaderCircle className="animate-spin" aria-hidden /> : <Play aria-hidden />}
            Run /{ready.name}
          </Button>
        ) : (
          <Button onClick={plainSend} disabled={!text.trim() || busy !== null} className="rounded-control">
            {busy === "send" ? <LoaderCircle className="animate-spin" aria-hidden /> : <Send aria-hidden />}
            {internal ? "Add note" : "Send"}
          </Button>
        )}
      </div>
      <Status notice={notice} />
    </section>
  );
}

function Status({ notice }: { notice: Notice | null }) {
  if (!notice) return null;
  return (
    <p
      role={notice.tone === "error" ? "alert" : "status"}
      className={`text-footnote ${notice.tone === "error" ? "text-danger" : "text-ink-secondary"}`}
    >
      {notice.text}
    </p>
  );
}

function message(err: unknown, fallback: string): string {
  return err instanceof ApiError || err instanceof Error ? err.message : fallback;
}

function describeSend(result: SendMessageResult, internal: boolean): Notice {
  if (internal) return { tone: "ok", text: "Note added. It was not sent to the customer." };
  const where = CHANNEL_LABEL[result.channel as SourceChannel | "internal"] ?? result.channel;
  if (!result.delivered) {
    return {
      tone: "error",
      text: `Saved, but not delivered on ${where}${result.error ? `: ${result.error}` : ""}. It will retry.`,
    };
  }
  if (result.simulated) return { tone: "ok", text: `Delivered to the ${where} test sink, since no ${where} bot is running here.` };
  return { tone: "ok", text: `Sent on ${where}.` };
}
