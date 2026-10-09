import { Cable, Headphones, Laptop, Monitor } from "lucide-react";
import Link from "next/link";

import { ChannelStack } from "@/components/ticket/glyphs";
import {
  PRIORITY_TONE,
  Pass,
  PassBottom,
  PassBrand,
  PassFields,
  PassPerson,
  PassPill,
  PassTear,
  PassTop,
  passDate,
} from "@/components/ticket/pass";
import type { Ticket } from "@/lib/api";
import { CHANNEL_LABEL, FLAG_LABEL, PRIORITY_LABEL, STATUS_LABEL, relative } from "@/lib/format";

const DEVICE_ICON = { laptop: Laptop, desktop: Monitor, headphones: Headphones, accessory: Cable } as const;
const CATEGORY_LABEL = { hardware: "Hardware", software: "Software", unknown: "Unknown" } as const;

// Cards after this one enter together, so a long inbox doesn't keep animating.
const MAX_STAGGER = 14;

/**
 * One inbox ticket as a pass (§11.3), the whole card in its priority's colour: the ServiceMesh line and
 * the date, the priority, the device and the ticket number on top; the tear line; then the title, the
 * customer and device, Channel / Type / Status, flags, the +N follow-up count, the search "why", and the
 * customer with the time. The same text and metadata as ever.
 */
export function TicketCard({
  ticket,
  highlighted,
  note,
  index = 0,
}: {
  ticket: Ticket;
  highlighted: boolean;
  /** A search result's one-line "why this matched" (§7.4). */
  note?: string;
  /** Position in the grid, for the entrance stagger. */
  index?: number;
}) {
  const device = ticket.product
    ? [ticket.product.model_name, ticket.product.color].filter(Boolean).join(", ") + ` · ${ticket.product.serial_number}`
    : null;
  const who = [ticket.customer?.full_name, device].filter(Boolean).join(" — ");
  const Icon = ticket.product ? DEVICE_ICON[ticket.product.category] : Cable;
  const tone = PRIORITY_TONE[ticket.priority];

  return (
    <li style={{ animationDelay: `${Math.min(index, MAX_STAGGER) * 35}ms` }} className="animate-card-in">
      <Link
        href={`/tickets/${ticket.id}`}
        prefetch={false}
        aria-label={`${ticket.ticket_number}: ${ticket.title}`}
        className="group block h-full rounded-[26px] outline-none focus-visible:ring-[3px] focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
      >
        <Pass
          tone={tone}
          className={`h-full transition-[transform,box-shadow] duration-200 ease-out group-hover:-translate-y-1 group-hover:shadow-[0_24px_40px_-22px_color-mix(in_oklab,var(--p1)_85%,transparent)] group-active:translate-y-0 ${
            highlighted ? "animate-arrive" : ""
          }`}
        >
          <PassTop tone={tone} className="px-5 pt-4 pb-5">
            <PassBrand right={passDate(ticket.updated_at)} />
            <div className="mt-4 flex items-center gap-2">
              <PassPill>{PRIORITY_LABEL[ticket.priority]}</PassPill>
              <span
                className="grid size-6 place-items-center rounded-full bg-white/25 transition-transform duration-200 group-hover:-rotate-6"
                title={ticket.product ? ticket.product.category : "No verified device"}
              >
                <Icon className="size-3.5" aria-hidden />
              </span>
            </div>
            <p className="mt-3 truncate text-[16px] font-semibold">
              {ticket.product ? ticket.product.model_name : "No verified device"}
            </p>
            <p className="text-[24px] leading-tight font-extrabold tracking-[-0.03em] whitespace-nowrap tabular-nums">
              {ticket.ticket_number}
            </p>
          </PassTop>
          <PassTear />
          <PassBottom className="gap-3 px-5 pt-4 pb-4">
            <div className="space-y-1">
              <h3 className="line-clamp-2 text-[15px] leading-snug font-bold tracking-[-0.01em]">{ticket.title}</h3>
              {who ? <p className="line-clamp-2 text-footnote opacity-70">{who}</p> : null}
            </div>
            <PassFields
              fields={[
                { label: "Channel", value: CHANNEL_LABEL[ticket.source_channel] },
                { label: "Type", value: CATEGORY_LABEL[ticket.category] },
                { label: "Status", value: <span title={STATUS_LABEL[ticket.status]}>{STATUS_LABEL[ticket.status]}</span> },
              ]}
            />
            {ticket.flags.length || ticket.duplicate_count > 0 || ticket.channels.length > 1 ? (
              <div className="flex flex-wrap items-center gap-1.5">
                {ticket.channels.length > 1 ? <ChannelStack channels={ticket.channels} /> : null}
                {ticket.flags.map((flag) => (
                  <span key={flag} className="rounded-full bg-white/80 px-2 py-0.5 text-[12px] font-semibold text-warning ring-1 ring-warning/25 dark:bg-white/10">
                    {FLAG_LABEL[flag] ?? flag}
                  </span>
                ))}
                {ticket.duplicate_count > 0 ? (
                  <span
                    className="rounded-full bg-white/80 px-2 py-0.5 text-[12px] font-bold tabular-nums text-accent ring-1 ring-accent/20 dark:bg-white/10"
                    title={`${ticket.duplicate_count} follow-up${ticket.duplicate_count === 1 ? "" : "s"} merged by duplicate detection`}
                  >
                    +{ticket.duplicate_count}
                  </span>
                ) : null}
              </div>
            ) : null}
            {note ? <p className="text-footnote font-medium text-accent">{note}</p> : null}
            <div className="mt-auto">
              <PassPerson
                name={ticket.customer?.full_name}
                trailing={<time dateTime={ticket.updated_at}>{relative(ticket.updated_at)}</time>}
              />
            </div>
          </PassBottom>
        </Pass>
      </Link>
    </li>
  );
}
