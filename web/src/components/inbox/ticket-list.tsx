"use client";

import { useRef, type KeyboardEvent } from "react";

import { ChannelStack } from "@/components/ticket/channel";
import { DeviceIcon, PriorityText, StatusChip } from "@/components/ticket/chips";
import type { Schemas } from "@/lib/api";
import { fullTime, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

type TicketRow = Schemas["TicketRow"];

export function TicketList({
  tickets,
  selectedId,
  onSelect,
  now,
}: {
  tickets: TicketRow[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  now: number;
}) {
  const rows = useRef(new Map<string, HTMLButtonElement>());

  // Up and down move through the list, like Mail.
  function onKeyDown(event: KeyboardEvent<HTMLUListElement>) {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    event.preventDefault();
    const index = tickets.findIndex((t) => t.id === selectedId);
    const next = tickets[Math.min(tickets.length - 1, Math.max(0, index + (event.key === "ArrowDown" ? 1 : -1)))];
    if (next) {
      onSelect(next.id);
      rows.current.get(next.id)?.focus();
    }
  }

  return (
    <ul aria-label="Tickets" onKeyDown={onKeyDown} className="px-2 pb-2">
      {tickets.map((ticket) => {
        const selected = ticket.id === selectedId;
        const device = ticket.device
          ? [ticket.device.model_name, ticket.device.color].filter(Boolean).join(", ")
          : "Device not verified";
        return (
          <li key={ticket.id} className="border-b border-hairline/60 last:border-b-0">
            <button
              ref={(el) => {
                if (el) rows.current.set(ticket.id, el);
                else rows.current.delete(ticket.id);
              }}
              type="button"
              aria-current={selected ? "true" : undefined}
              onClick={() => onSelect(ticket.id)}
              className={cn(
                "my-1 flex w-full gap-3 rounded-card px-2.5 py-3 text-left transition-colors",
                selected ? "bg-accent/10" : "hover:bg-canvas",
              )}
            >
              <DeviceIcon category={ticket.device?.category} />
              <span className="min-w-0 flex-1">
                <span className="flex items-baseline gap-2">
                  <span className="min-w-0 flex-1 truncate font-semibold text-ink">{ticket.title}</span>
                  {/* §11.3 puts the priority here; a narrower list moves it below so the title fits. */}
                  <span className="hidden xl:inline-flex">
                    <PriorityText priority={ticket.priority} />
                  </span>
                  <time
                    dateTime={ticket.updated_at}
                    title={fullTime(ticket.updated_at)}
                    className="shrink-0 text-footnote text-ink-secondary tabular-nums"
                  >
                    {timeAgo(ticket.updated_at, now)}
                  </time>
                </span>
                <span className="mt-0.5 block truncate text-subheadline text-ink-secondary">
                  {ticket.customer.name ?? "Unknown customer"} — {device}
                </span>
                <span className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                  <span className="text-footnote text-ink-secondary tabular-nums">{ticket.ticket_number}</span>
                  <span className="inline-flex items-center gap-1.5">
                    <ChannelStack channels={ticket.channels} />
                    {ticket.duplicate_count > 0 && (
                      <span
                        title={`${ticket.duplicate_count} follow-up${ticket.duplicate_count === 1 ? "" : "s"} merged`}
                        className="text-footnote font-semibold text-ink-secondary tabular-nums"
                      >
                        +{ticket.duplicate_count}
                      </span>
                    )}
                  </span>
                  <StatusChip status={ticket.status} />
                  <span className="xl:hidden">
                    <PriorityText priority={ticket.priority} />
                  </span>
                </span>
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
