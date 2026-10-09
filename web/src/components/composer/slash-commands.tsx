"use client";

// The composer's slash commands (ARCHITECTURE.md §7.5): the "/" menu, and the inline progress of a
// running command. The menu is cmdk's listbox; the composer's textarea keeps the focus and drives
// the selection with the arrow keys, so typing never leaves the box.

import { Command } from "cmdk";
import { Check, CircleAlert, LoaderCircle, Minus, TriangleAlert, X } from "lucide-react";

import type { Command as SlashCommand, CommandEvent, CommandStep } from "@/lib/commands";

export function CommandMenu({
  query,
  matches,
  value,
  onValueChange,
  onChoose,
  loadError,
  loading,
}: {
  /** What the agent has typed after the "/". */
  query: string;
  matches: SlashCommand[];
  value: string;
  onValueChange: (value: string) => void;
  onChoose: (command: SlashCommand) => void;
  /** Set when GET /api/commands failed, so the menu says why it is empty. */
  loadError: string | null;
  /** GET /api/commands hasn't answered yet. */
  loading: boolean;
}) {
  return (
    <Command
      label="Slash commands"
      shouldFilter={false}
      value={value}
      onValueChange={onValueChange}
      className="overflow-hidden rounded-control bg-surface-raised shadow-raised animate-in fade-in slide-in-from-bottom-1"
    >
      <Command.List className="max-h-56 overflow-y-auto p-1">
        {matches.length ? (
          matches.map((command) => (
            <Command.Item
              key={command.name}
              value={command.name}
              onSelect={() => onChoose(command)}
              className="flex cursor-pointer flex-col gap-0.5 rounded-[8px] px-3 py-2 text-subheadline text-ink outline-none data-[selected=true]:bg-accent/12"
            >
              <span>
                <span className="font-medium">{command.usage}</span>
                {command.is_builtin ? null : <span className="text-footnote text-ink-secondary"> · yours</span>}
              </span>
              <span className="text-footnote text-ink-secondary">
                {command.description}
                {command.example ? ` E.g. /${command.name} ${command.example}` : ""}
              </span>
            </Command.Item>
          ))
        ) : (
          <p className="px-3 py-2 text-footnote text-ink-secondary">
            {loading
              ? "Loading commands…"
              : loadError
                ? `Couldn’t load the commands: ${loadError}`
                : `No command named /${query}.`}
          </p>
        )}
      </Command.List>
    </Command>
  );
}

// The commands' steps, as the backend names them (app/brain/commands.py).
const STEP_LABEL: Record<string, string> = {
  ticket: "Ticket",
  device: "Device",
  service: "Service",
  warranty: "Warranty",
  price: "Price",
  ask: "Customer",
  status: "Ticket status",
  reply: "Reply",
  playbook: "Playbook",
  next: "Next step",
  summary: "Summary",
  question: "Question",
  part: "Part",
  stock: "Stock",
  booking: "Booking",
  priority: "Priority",
  notify: "Admins",
  tools: "Tools",
  tool: "Tool call",
};

function StepIcon({ status }: { status: string }) {
  if (status === "failed") return <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-label="Failed" />;
  if (status === "warning") return <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-label="Warning" />;
  if (status === "skipped") return <Minus className="mt-0.5 size-4 shrink-0 text-ink-secondary" aria-label="Skipped" />;
  return <Check className="mt-0.5 size-4 shrink-0 text-success" aria-label="Done" />;
}

export type CommandRun = {
  command: string;
  args: string;
  state: "running" | "done" | "error";
  events: CommandEvent[];
};

/** The streamed steps of a command, inline under the composer, until it is done or fails. */
export function CommandRunPanel({ run, onDismiss }: { run: CommandRun; onDismiss: () => void }) {
  const steps = run.events.filter((e): e is CommandStep => e.type === "step");
  const end = run.events.find((e) => e.type === "done" || e.type === "error");
  return (
    <section
      aria-label={`/${run.command} progress`}
      aria-live="polite"
      className="space-y-2 rounded-control bg-canvas p-3 dark:bg-surface-raised"
    >
      <div className="flex items-center gap-2">
        <p className="text-subheadline font-medium text-ink">
          /{run.command}
          {run.args ? <span className="font-normal text-ink-secondary"> {run.args}</span> : null}
        </p>
        {run.state !== "running" ? (
          <button
            type="button"
            onClick={onDismiss}
            aria-label="Dismiss"
            className="ml-auto grid size-7 place-items-center rounded-control text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent"
          >
            <X className="size-4" aria-hidden />
          </button>
        ) : null}
      </div>
      <ol className="space-y-1.5">
        {steps.map((step, index) => (
          <li key={`${step.step}-${index}`} className="flex gap-2 text-subheadline">
            <StepIcon status={step.status} />
            <span className="min-w-0">
              <span className="font-medium text-ink">{STEP_LABEL[step.step] ?? step.step}</span>{" "}
              <span className="break-words text-ink-secondary">{step.detail}</span>
            </span>
          </li>
        ))}
        {run.state === "running" ? (
          <li className="flex items-center gap-2 text-subheadline text-ink-secondary">
            <LoaderCircle className="size-4 shrink-0 animate-spin" aria-hidden />
            Working…
          </li>
        ) : null}
      </ol>
      {end?.type === "done" ? (
        <p role="status" className="whitespace-pre-wrap rounded-control bg-success/12 px-3 py-2 text-subheadline text-ink">
          {end.message}
        </p>
      ) : null}
      {end?.type === "error" ? (
        <p role="alert" className="flex gap-2 rounded-control bg-danger/10 px-3 py-2 text-subheadline text-ink">
          <CircleAlert className="mt-0.5 size-4 shrink-0 text-danger" aria-hidden />
          {end.message}
        </p>
      ) : null}
    </section>
  );
}
