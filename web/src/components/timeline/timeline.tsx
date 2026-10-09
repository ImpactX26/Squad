import { Bot, StickyNote } from "lucide-react";

import { ChannelGlyph } from "@/components/ticket/glyphs";
import type { SourceChannel, TimelineEntry } from "@/lib/api";
import { CHANNEL_LABEL, PRIORITY_LABEL, STATUS_LABEL, absolute, relative } from "@/lib/format";

// How long after a customer message the followup event may be written and still belong to it.
// Intake records the message first, then runs the model, then adds the follow-up (section 7.2).
const FOLLOWUP_WINDOW_MS = 120_000;
const CLOCK_SKEW_MS = 5_000;

const isChannel = (value: string | null): value is SourceChannel =>
  value === "web" || value === "telegram" || value === "discord" || value === "email";

export type TimelineItem =
  | { kind: "message"; entry: TimelineEntry; followUp: boolean }
  | { kind: "event"; entry: TimelineEntry };

/** The generated type marks fields with server defaults as optional; the API always sends them. */
const payloadOf = (entry: TimelineEntry): Record<string, unknown> => entry.payload ?? {};

function channelOf(event: TimelineEntry): string | null {
  const channel = payloadOf(event).channel;
  return typeof channel === "string" ? channel : null;
}

/** The customer message a followup event describes: the latest unclaimed one just before it, on that channel. */
function messageFor(event: TimelineEntry, entries: TimelineEntry[], claimed: Set<string>): TimelineEntry | undefined {
  const channel = channelOf(event);
  const at = new Date(event.at).getTime();
  return entries
    .filter((m) => m.kind === "message" && m.sender_type === "customer" && !claimed.has(m.id))
    .filter((m) => (channel ? m.channel === channel : true))
    .filter((m) => {
      const age = at - new Date(m.at).getTime();
      return age >= -CLOCK_SKEW_MS && age <= FOLLOWUP_WINDOW_MS;
    })
    .at(-1);
}

/**
 * Fold each followup event into the customer message it describes, so the timeline shows that
 * message with a follow-up marker (section 11.3) instead of the message plus a separate line.
 * An event with no matching message stays visible as its own line.
 */
export function buildItems(entries: TimelineEntry[]): TimelineItem[] {
  const claimed = new Set<string>();
  const folded = new Set<string>();
  for (const event of entries) {
    if (event.kind !== "event" || event.type !== "followup") continue;
    const match = messageFor(event, entries, claimed);
    if (match) {
      claimed.add(match.id);
      folded.add(event.id);
    }
  }
  return entries
    .filter((e) => !folded.has(e.id))
    .map((entry): TimelineItem =>
      entry.kind === "message" ? { kind: "message", entry, followUp: claimed.has(entry.id) } : { kind: "event", entry },
    );
}

export function Timeline({ entries, customerName }: { entries: TimelineEntry[]; customerName: string }) {
  const items = buildItems(entries);
  if (items.length === 0) {
    return (
      <p className="rounded-card bg-surface px-4 py-6 text-subheadline text-ink-secondary">
        No messages on this ticket yet.
      </p>
    );
  }
  return (
    <ol className="space-y-4" aria-live="polite" aria-label="Timeline">
      {items.map((item) =>
        item.kind === "message" ? (
          <MessageRow key={item.entry.id} entry={item.entry} followUp={item.followUp} customerName={customerName} />
        ) : (
          <EventRow key={item.entry.id} entry={item.entry} />
        ),
      )}
    </ol>
  );
}

function Disc({ channel }: { channel: string | null }) {
  return (
    <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-full bg-canvas text-ink-secondary dark:bg-surface-raised">
      {isChannel(channel) ? (
        <ChannelGlyph channel={channel} />
      ) : (
        <StickyNote className="size-3.5" aria-label="Internal" role="img" />
      )}
    </span>
  );
}

function senderName(entry: TimelineEntry, customerName: string): string {
  switch (entry.sender_type) {
    case "customer":
      return customerName;
    case "agent":
      return entry.sender_name ?? "Agent";
    case "ai":
      return "Assistant";
    case "technician":
      return entry.sender_name ?? "Technician";
    default:
      return "System";
  }
}

function MessageRow({
  entry,
  followUp,
  customerName,
}: {
  entry: TimelineEntry;
  followUp: boolean;
  customerName: string;
}) {
  const note = entry.is_internal_note === true;
  const edited = entry.body_original && entry.body_original !== entry.body;

  return (
    <li className="flex gap-3">
      <Disc channel={note ? null : (entry.channel ?? null)} />
      <div className={`min-w-0 flex-1 ${note ? "rounded-card bg-warning/10 px-3 py-2" : ""}`}>
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
          <span className="text-subheadline font-medium text-ink">{senderName(entry, customerName)}</span>
          {entry.sender_type === "ai" ? (
            <span className="inline-flex items-center gap-1 rounded-full bg-accent/12 px-2 py-0.5 text-footnote font-medium text-accent">
              <Bot className="size-3" aria-hidden /> AI
            </span>
          ) : null}
          {note ? <span className="text-footnote font-medium text-warning">Internal note, not sent</span> : null}
          {followUp ? (
            <span className="rounded-full bg-canvas px-2 py-0.5 text-footnote text-ink-secondary dark:bg-surface-raised">
              Follow-up
            </span>
          ) : null}
          {!note && isChannel(entry.channel ?? null) ? (
            <span className="text-footnote text-ink-secondary">via {CHANNEL_LABEL[entry.channel as SourceChannel]}</span>
          ) : null}
          <time
            className="ml-auto text-footnote tabular-nums text-ink-secondary"
            dateTime={entry.at}
            title={absolute(entry.at)}
          >
            {relative(entry.at)}
          </time>
        </div>
        <p className="mt-0.5 whitespace-pre-wrap break-words text-subheadline text-ink">{entry.body}</p>
        {edited ? (
          <details className="mt-1 text-footnote text-ink-secondary">
            <summary className="w-fit cursor-pointer rounded-sm hover:text-ink focus-visible:outline-2 focus-visible:outline-accent">
              Polished from the agent&apos;s note
            </summary>
            <p className="mt-1 whitespace-pre-wrap break-words">{entry.body_original}</p>
          </details>
        ) : null}
      </div>
    </li>
  );
}

const label = (map: Record<string, string>, key: string) => map[key] ?? key;

function describe(entry: TimelineEntry): string {
  const p = payloadOf(entry);
  const status = typeof p.status === "string" ? p.status : null;
  const priority = typeof p.priority === "string" ? p.priority : null;
  switch (entry.type) {
    case "created":
      return "Ticket created";
    case "status_changed":
      return status ? `Status changed to ${label(STATUS_LABEL, status)}` : "Status changed";
    case "priority_raised":
      return priority ? `Priority raised to ${label(PRIORITY_LABEL, priority)}` : "Priority raised";
    case "followup":
      return typeof p.note === "string" ? p.note : "Customer followed up";
    case "updated":
      return priority ? `Priority set to ${label(PRIORITY_LABEL, priority)}` : "Ticket updated";
    default: {
      const text = (entry.type ?? "event").replace(/_/g, " ");
      return text.charAt(0).toUpperCase() + text.slice(1);
    }
  }
}

function EventRow({ entry }: { entry: TimelineEntry }) {
  const raw = payloadOf(entry).note;
  const note = typeof raw === "string" && entry.type !== "followup" ? raw : null;
  return (
    <li className="flex items-baseline gap-3 text-footnote text-ink-secondary">
      <span className="grid size-7 shrink-0 place-items-center" aria-hidden>
        <span className="size-1.5 rounded-full bg-hairline" />
      </span>
      <span className="min-w-0 flex-1">
        {describe(entry)}
        {note ? <span> — {note}</span> : null}
      </span>
      <time className="shrink-0 tabular-nums" dateTime={entry.at} title={absolute(entry.at)}>
        {relative(entry.at)}
      </time>
    </li>
  );
}
