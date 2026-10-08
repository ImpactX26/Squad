"use client";

import { AlertTriangle, ChevronLeft, MessagesSquare, Sparkles } from "lucide-react";
import type { ReactNode } from "react";

import { useConversation } from "@/components/inbox/data";
import { EmptyState } from "@/components/states";
import { ChannelBadge, ChannelGlyph } from "@/components/ticket/channel";
import { PriorityChip, StatusChip } from "@/components/ticket/chips";
import type { Schemas } from "@/lib/api";
import { fullTime, timeAgo } from "@/lib/format";
import type { MockMessage } from "@/mocks/inbox";
import { cn } from "@/lib/utils";

type TicketRow = Schemas["TicketRow"];

const FLAG_NOTES: Record<string, string> = {
  unverified_product: "Device not verified: the serial didn't match the catalog.",
  ownership_mismatch: "This serial is registered to another customer. Check before acting.",
};

export function TicketPreview({
  ticket,
  me,
  now,
  onBack,
}: {
  ticket: TicketRow;
  me: string;
  now: number;
  onBack: () => void;
}) {
  const assigned =
    ticket.assigned_agent_id === null ? "Unassigned" : ticket.assigned_agent_id === me ? "You" : "Another agent";

  return (
    <article aria-label={`Ticket ${ticket.ticket_number}`} className="mx-auto w-full max-w-3xl px-5 py-5 sm:px-8 sm:py-7">
      <button
        type="button"
        onClick={onBack}
        className="-ml-2 mb-3 inline-flex h-9 items-center gap-1 rounded-full pr-3 pl-2 text-subheadline text-accent md:hidden"
      >
        <ChevronLeft aria-hidden="true" className="size-4" />
        Inbox
      </button>

      <header>
        <p className="text-footnote text-ink-secondary tabular-nums">
          {ticket.ticket_number} · opened <time dateTime={ticket.created_at} title={fullTime(ticket.created_at)}>{timeAgo(ticket.created_at, now)}</time>
        </p>
        <h2 className="mt-1 text-title-2 font-semibold text-balance">{ticket.title}</h2>
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <StatusChip status={ticket.status} />
          <PriorityChip priority={ticket.priority} />
          {ticket.channels.map((channel) => (
            <ChannelBadge key={channel} channel={channel} />
          ))}
          {ticket.duplicate_count > 0 && (
            <span className="text-footnote font-semibold text-ink-secondary">
              +{ticket.duplicate_count} follow-up{ticket.duplicate_count === 1 ? "" : "s"}
            </span>
          )}
        </div>
      </header>

      {ticket.flags.filter((f) => FLAG_NOTES[f]).map((flag) => (
        <p key={flag} className="mt-4 flex items-start gap-2.5 rounded-card bg-warning/12 px-3.5 py-2.5 text-subheadline text-ink">
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
          {FLAG_NOTES[flag]}
        </p>
      ))}

      <dl className="mt-6 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
        <Fact label="Customer">{ticket.customer.name ?? "Unknown"}</Fact>
        <Fact label="Device">
          {ticket.device ? (
            <>
              {ticket.device.model_name}
              <span className="block text-footnote text-ink-secondary tabular-nums">{ticket.device.serial_number}</span>
            </>
          ) : (
            <span className="text-ink-secondary">Not verified</span>
          )}
        </Fact>
        <Fact label="Assigned">{assigned}</Fact>
        <Fact label="Updated">
          <time dateTime={ticket.updated_at} title={fullTime(ticket.updated_at)}>{timeAgo(ticket.updated_at, now)}</time>
        </Fact>
      </dl>

      <section aria-labelledby="summary" className="mt-7">
        <h3 id="summary" className="flex items-center gap-2 font-semibold">
          <Sparkles aria-hidden="true" className="size-4 text-accent" />
          AI summary
        </h3>
        <p className="mt-2 text-pretty text-ink">
          {ticket.ai_summary ?? <span className="text-ink-secondary">No summary yet.</span>}
        </p>
      </section>

      <section aria-labelledby="conversation" className="mt-8">
        <h3 id="conversation" className="font-semibold">Conversation</h3>
        <Conversation ticket={ticket} now={now} />
      </section>
    </article>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-footnote text-ink-secondary">{label}</dt>
      <dd className="mt-0.5 truncate text-subheadline font-semibold text-ink">{children}</dd>
    </div>
  );
}

function Conversation({ ticket, now }: { ticket: TicketRow; now: number }) {
  const conversation = useConversation(ticket);
  if (conversation.status === "not_built") {
    return (
      <EmptyState icon={MessagesSquare} title="The conversation isn't available yet" className="py-8">
        {conversation.reason}
      </EmptyState>
    );
  }
  return (
    <ol className="mt-4 space-y-4">
      {conversation.messages.map((message) => (
        <Bubble key={message.id} message={message} now={now} />
      ))}
    </ol>
  );
}

const SENDER: Record<MockMessage["sender_type"], string> = { customer: "Customer", ai: "AI", agent: "Agent" };

function Bubble({ message, now }: { message: MockMessage; now: number }) {
  const fromCustomer = message.sender_type === "customer";
  return (
    <li className={cn("flex flex-col", fromCustomer ? "items-start" : "items-end")}>
      <p className={cn("mb-1 flex items-center gap-1.5 px-1 text-footnote text-ink-secondary", !fromCustomer && "flex-row-reverse")}>
        <ChannelGlyph channel={message.channel} className="size-5" />
        <span className="font-semibold text-ink">
          {SENDER[message.sender_type]}
          {message.author && message.sender_type !== "ai" ? ` · ${message.author}` : ""}
        </span>
        <time dateTime={message.created_at} title={fullTime(message.created_at)} className="tabular-nums">
          {timeAgo(message.created_at, now)}
        </time>
      </p>
      <p
        className={cn(
          "max-w-[85%] rounded-panel px-4 py-2.5 text-subheadline text-pretty",
          message.sender_type === "customer" && "rounded-tl-control bg-canvas text-ink",
          message.sender_type === "ai" && "rounded-tr-control bg-accent/10 text-ink",
          message.sender_type === "agent" && "rounded-tr-control bg-accent text-on-accent",
        )}
      >
        {message.body}
      </p>
    </li>
  );
}
