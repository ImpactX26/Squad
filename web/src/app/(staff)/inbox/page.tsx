"use client";

// The inbox (ARCHITECTURE.md §11.2): a grid of ticket passes (§11.3), 3 to 5 a row on a desktop. It
// reads GET /api/tickets with the §10 filters and refreshes itself from /ws/staff, so a ticket intake
// creates appears without a reload (§9). Cards link to /tickets/[id].
//
// The search box runs a natural-language search (§7.4, POST /api/search) instead of the filters.
// The words live in the URL (/inbox?q=...), so the ⌘K palette can open its results here and the
// back button leaves the search.

import { ArrowRight, Inbox, RotateCcw, Search, SearchX, X } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, errorMessage } from "@/components/states";
import { LiveBadge } from "@/components/ticket/live-badge";
import { TicketCard } from "@/components/ticket/ticket-card";
import { PRIORITY_TONE, passStyle } from "@/components/ticket/pass";
import { Button } from "@/components/ui/button";
import {
  type SourceChannel,
  type Ticket,
  type TicketCategory,
  type TicketFilters,
  type TicketPriority,
  type TicketStatus,
  api,
} from "@/lib/api";
import { CHANNEL_LABEL, PRIORITY_LABEL, STATUS_LABEL } from "@/lib/format";
import { useTicketSearch } from "@/lib/search";
import { type SocketStatus, type StaffEvent, openStaffSocket } from "@/lib/ws";

// Events that change which tickets belong in the list, or their order (§9).
const REFRESHING_EVENTS = new Set<StaffEvent["type"]>([
  "ticket.created",
  "ticket.updated",
  "ticket.followup",
]);

const STATUSES: TicketStatus[] = [
  "new",
  "in_progress",
  "awaiting_customer",
  "awaiting_payment",
  "scheduled",
  "resolved",
  "closed",
];
const PRIORITIES: TicketPriority[] = ["urgent", "high", "medium", "low"];
const CHANNELS: SourceChannel[] = ["web", "telegram", "discord", "email"];
const CATEGORIES: TicketCategory[] = ["hardware", "software", "unknown"];

const sentence = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

// The chips' counts are over every ticket, read a page at a time. Past this many the numbers would be
// partial, so they are left off rather than shown wrong.
const COUNT_PAGE = 100;
const COUNT_PAGES = 5;

type Counts = {
  all: number;
  open: number;
  status: Map<string, number>;
  priority: Map<string, number>;
  category: Map<string, number>;
  channel: Map<string, number>;
};

/** How many tickets each filter chip would match, or null when there are too many to count here. */
async function countTickets(): Promise<Counts | null> {
  const seen: Ticket[] = [];
  let total = 0;
  for (let page = 0; page < COUNT_PAGES; page++) {
    const body = await api.tickets({ limit: COUNT_PAGE, offset: page * COUNT_PAGE });
    seen.push(...body.tickets);
    total = body.total;
    if (seen.length >= total || body.tickets.length < COUNT_PAGE) break;
  }
  if (seen.length < total) return null;
  const tally = (key: (t: Ticket) => string) => {
    const map = new Map<string, number>();
    for (const ticket of seen) map.set(key(ticket), (map.get(key(ticket)) ?? 0) + 1);
    return map;
  };
  return {
    all: seen.length,
    open: seen.filter((t) => t.status !== "resolved" && t.status !== "closed").length,
    status: tally((t) => t.status),
    priority: tally((t) => t.priority),
    category: tally((t) => t.category),
    channel: tally((t) => t.source_channel),
  };
}

export default function InboxPage() {
  // useSearchParams needs a Suspense boundary when the page is prerendered (Next's docs).
  return (
    <Suspense fallback={<LoadingState label="Loading the inbox" rows={6} />}>
      <InboxFromUrl />
    </Suspense>
  );
}

function InboxFromUrl() {
  const q = useSearchParams().get("q");
  // A new search remounts the inbox with it, so the words and the results always match the URL.
  return <InboxContent key={q ?? ""} initialQuery={q} />;
}

function InboxContent({ initialQuery }: { initialQuery: string | null }) {
  const router = useRouter();
  const search = useTicketSearch(initialQuery);
  const [words, setWords] = useState(initialQuery ?? "");
  const searching = search.result.state !== "idle";
  const [filters, setFilters] = useState<TicketFilters>({ open_only: true, limit: 50 });
  const [tickets, setTickets] = useState<Ticket[]>([]);
  const [total, setTotal] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [firstLoad, setFirstLoad] = useState(true);
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const [flash, setFlash] = useState<Set<string>>(new Set());
  const [chipCounts, setChipCounts] = useState<Counts | null>(null);
  // Bumped by /ws/staff, so a live event refetches without the socket effect depending on filters.
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const body = await api.tickets(filters);
        if (cancelled) return;
        setTickets(body.tickets);
        setTotal(body.total);
        setError(null);
      } catch (err: unknown) {
        if (!cancelled) setError(errorMessage(err, "Couldn’t load the inbox."));
      } finally {
        // Refetches swap the rows in without a loading flicker; only the first one shows it.
        if (!cancelled) setFirstLoad(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [filters, revision]);

  // The chips' counts follow the same live refreshes as the list.
  useEffect(() => {
    let cancelled = false;
    countTickets()
      .then((counts) => {
        if (!cancelled) setChipCounts(counts);
      })
      .catch(() => {
        if (!cancelled) setChipCounts(null); // the list shows its own error; the chips just go without numbers
      });
    return () => {
      cancelled = true;
    };
  }, [revision]);

  useEffect(() => {
    return openStaffSocket((event) => {
      if (!REFRESHING_EVENTS.has(event.type)) return;
      const id = typeof event.data.ticket_id === "string" ? event.data.ticket_id : null;
      if (id) {
        // Highlight what just changed, so a live arrival is visible during the demo.
        setFlash((current) => new Set(current).add(id));
        setTimeout(
          () =>
            setFlash((current) => {
              const next = new Set(current);
              next.delete(id);
              return next;
            }),
          4000,
        );
      }
      setRevision((current) => current + 1);
    }, setSocket);
  }, []);

  const counts = useMemo(() => {
    const byPriority = new Map<string, number>();
    for (const ticket of tickets) {
      byPriority.set(ticket.priority, (byPriority.get(ticket.priority) ?? 0) + 1);
    }
    return byPriority;
  }, [tickets]);

  function toggle<T extends string>(key: keyof TicketFilters, value: T) {
    setFilters((current) => {
      const list = (current[key] as T[] | undefined) ?? [];
      const next = list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
      return { ...current, [key]: next.length ? next : undefined };
    });
  }

  function runSearch(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = words.trim();
    router.replace(trimmed ? `/inbox?q=${encodeURIComponent(trimmed)}` : "/inbox");
  }

  const retry = () => setRevision((current) => current + 1);

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end gap-x-4 gap-y-2">
        <h1 className="text-large-title font-semibold">Inbox</h1>
        <p className="pb-1 text-subheadline text-ink-secondary">
          {searching
            ? search.result.state === "done"
              ? `${search.result.response.results.length} found`
              : "Searching…"
            : firstLoad
              ? "Loading…"
              : `${tickets.length} of ${total} ticket${total === 1 ? "" : "s"}`}
          {!searching && counts.get("urgent") ? ` · ${counts.get("urgent")} urgent` : ""}
        </p>
        <LiveBadge status={socket} className="ml-auto pb-1.5" />
      </header>

      <form role="search" onSubmit={runSearch} className="flex items-center gap-2">
        <label className="flex h-14 min-w-0 flex-1 items-center gap-3 rounded-[22px] bg-surface pr-2.5 pl-5 ring-[1.5px] ring-accent/20 shadow-[0_12px_32px_-22px_color-mix(in_oklab,var(--accent)_60%,transparent)] transition-shadow focus-within:ring-2 focus-within:ring-accent">
          <Search className="size-5 shrink-0 text-ink-secondary" aria-hidden />
          <span className="sr-only">Search tickets in plain words</span>
          <input
            type="search"
            enterKeyHint="search"
            value={words}
            onChange={(event) => setWords(event.target.value)}
            placeholder="Search in plain words, a ticket number or a serial (AX14-7F3K92)"
            className="h-full w-full min-w-0 bg-transparent text-[16px] text-ink outline-none placeholder:text-ink-secondary/80 [&::-webkit-search-cancel-button]:hidden"
          />
          {words.trim() ? (
            <button
              type="submit"
              aria-label="Search"
              className="grid size-9 shrink-0 place-items-center rounded-full bg-accent text-on-accent outline-none transition active:scale-90 focus-visible:ring-2 focus-visible:ring-violet focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
            >
              <ArrowRight className="size-[18px]" aria-hidden />
            </button>
          ) : (
            <kbd
              title="Press ⌘K (Ctrl + K) to search from any page"
              className="hidden shrink-0 rounded-[9px] bg-canvas px-2 py-1 font-sans text-[13px] font-semibold text-ink-secondary sm:inline dark:bg-surface-raised"
            >
              ⌘K
            </kbd>
          )}
        </label>
        {searching ? (
          <Button type="button" variant="ghost" onClick={() => router.replace("/inbox")} className="h-14 rounded-[22px] px-4">
            <X aria-hidden /> Clear
          </Button>
        ) : null}
      </form>

      {searching ? (
        <SearchResults search={search} />
      ) : (
        <>
          <section aria-label="Filters" className="space-y-3">
            <FilterLine label="Status">
              <Chip active={!filters.open_only && !filters.status?.length} count={chipCounts?.all}
                    onClick={() => setFilters((c) => ({ ...c, status: undefined, open_only: undefined }))}>
                All tickets
              </Chip>
              <Chip active={filters.open_only === true} count={chipCounts?.open} dot="var(--success)"
                    onClick={() => setFilters((c) => ({ ...c, open_only: c.open_only ? undefined : true }))}>
                Open only
              </Chip>
              {STATUSES.map((status) => (
                <Chip key={status} active={filters.status?.includes(status) ?? false} count={chipCounts?.status.get(status) ?? 0}
                      counted={chipCounts !== null} onClick={() => toggle("status", status)}>
                  {STATUS_LABEL[status]}
                </Chip>
              ))}
              <button
                type="button"
                onClick={() => setFilters({ open_only: true, limit: 50 })}
                aria-label="Reset filters"
                title="Reset filters"
                className="ml-auto grid size-9 place-items-center rounded-full text-ink-secondary outline-none transition hover:bg-surface hover:text-ink active:rotate-[-40deg] focus-visible:ring-2 focus-visible:ring-violet"
              >
                <RotateCcw className="size-[18px]" aria-hidden />
              </button>
            </FilterLine>
            <FilterLine label="Priority">
              {PRIORITIES.map((priority) => (
                <Chip key={priority} tone={PRIORITY_TONE[priority]} active={filters.priority?.includes(priority) ?? false}
                      count={chipCounts?.priority.get(priority) ?? 0} counted={chipCounts !== null}
                      onClick={() => toggle("priority", priority)}>
                  {PRIORITY_LABEL[priority]}
                </Chip>
              ))}
            </FilterLine>
            <FilterLine label="Type">
              {CATEGORIES.map((category) => (
                <Chip key={category} active={filters.category?.includes(category) ?? false}
                      count={chipCounts?.category.get(category) ?? 0} counted={chipCounts !== null}
                      onClick={() => toggle("category", category)}>
                  {sentence(category)}
                </Chip>
              ))}
              <span aria-hidden className="mx-1 hidden h-6 w-px bg-hairline sm:block" />
              <span className="w-full text-[12px] font-bold tracking-[0.08em] text-ink-secondary uppercase sm:w-auto sm:pr-1">
                Channel
              </span>
              {CHANNELS.map((channel) => (
                <Chip key={channel} active={filters.channel?.includes(channel) ?? false}
                      count={chipCounts?.channel.get(channel) ?? 0} counted={chipCounts !== null}
                      onClick={() => toggle("channel", channel)}>
                  {CHANNEL_LABEL[channel]}
                </Chip>
              ))}
            </FilterLine>
            <FilterLine label="Assigned">
              <Chip active={filters.assignee === "me"}
                    onClick={() => setFilters((c) => ({ ...c, assignee: c.assignee === "me" ? undefined : "me" }))}>
                Assigned to me
              </Chip>
              <Chip active={filters.assignee === "unassigned"}
                    onClick={() =>
                      setFilters((c) => ({ ...c, assignee: c.assignee === "unassigned" ? undefined : "unassigned" }))}>
                Unassigned
              </Chip>
            </FilterLine>
          </section>

          {firstLoad ? <LoadingState label="Loading tickets" rows={6} /> : null}
          {!firstLoad && error && tickets.length === 0 ? <ErrorState message={error} onRetry={retry} /> : null}
          {!firstLoad && error && tickets.length > 0 ? <InlineError message={error} onRetry={retry} /> : null}
          {!firstLoad && !error && tickets.length === 0 ? (
            <EmptyState
              icon={Inbox}
              title="No tickets match these filters."
              hint="New tickets appear here live as customers write in on any channel."
            />
          ) : null}

          {tickets.length ? (
            <TicketGrid label="Tickets">
              {tickets.map((ticket, index) => (
                <TicketCard key={ticket.id} ticket={ticket} index={index} highlighted={flash.has(ticket.id)} />
              ))}
            </TicketGrid>
          ) : null}
        </>
      )}
    </div>
  );
}

function SearchResults({ search }: { search: ReturnType<typeof useTicketSearch> }) {
  const { result } = search;
  if (result.state === "loading") return <LoadingState label="Searching" rows={4} />;
  if (result.state === "error") {
    return <ErrorState message={result.message} onRetry={() => search.submit(result.query)} />;
  }
  if (result.state !== "done") return null;
  const { response } = result;
  return (
    <section aria-label="Search results" className="space-y-3">
      <p className="text-footnote text-ink-secondary">
        {response.notice ? <span className="text-warning">{response.notice} </span> : null}
        Searched: {response.interpretation.summary}
      </p>
      {response.results.length === 0 ? (
        <EmptyState
          icon={SearchX}
          title={`No tickets match “${response.query}”.`}
          hint="Try fewer words, or a ticket number like SR-2026-00042."
        />
      ) : (
        <TicketGrid label="Search results">
          {response.results.map(({ ticket, why }, index) => (
            <TicketCard key={ticket.id} ticket={ticket} index={index} highlighted={false} note={why} />
          ))}
        </TicketGrid>
      )}
    </section>
  );
}

/**
 * Ticket passes (§11.3), each at least about 290px wide so its Channel / Type / Status fields read in full:
 * 3 a row beside the Any Questions sidebar on a laptop, 4 with it closed, 5 on a wide monitor. The columns
 * follow the grid's own width, not the window's; a narrow screen gets fewer, never cramped ones.
 */
function TicketGrid({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="@container">
      <ul
        aria-label={label}
        className="grid grid-cols-1 gap-5 @min-[600px]:grid-cols-2 @min-[900px]:grid-cols-3 @min-[1200px]:grid-cols-4 @min-[1520px]:grid-cols-5"
      >
        {children}
      </ul>
    </div>
  );
}

/** One labelled row of filter chips: the label in small capitals, the chips wrapping beside it. */
function FilterLine({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div role="group" aria-label={label} className="grid gap-2 sm:grid-cols-[6rem_minmax(0,1fr)] sm:items-start">
      <span className="text-[12px] font-bold tracking-[0.08em] text-ink-secondary uppercase sm:pt-2.5">{label}</span>
      {/* The chips wrap in their own column, so a second line lines up under the first chip. */}
      <div className="flex flex-wrap items-center gap-2">{children}</div>
    </div>
  );
}

/**
 * A filter chip: "In progress (14)". Plain chips are white and turn indigo when on; a priority chip carries
 * its pass colour (§11.3) as a dot, a tint and an outline, and fills with it when on.
 */
function Chip({
  active,
  onClick,
  children,
  count,
  counted = count !== undefined,
  tone,
  dot,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
  /** How many tickets it matches; left off while unknown. */
  count?: number;
  counted?: boolean;
  tone?: (typeof PRIORITY_TONE)[keyof typeof PRIORITY_TONE];
  /** A status dot's colour, for a plain chip. */
  dot?: string;
}) {
  const base =
    "inline-flex h-9 items-center gap-2 rounded-full px-3.5 text-[14px] font-semibold tracking-[0.01em] outline-none " +
    "transition-[background-color,color,box-shadow,transform] duration-150 active:scale-95 focus-visible:ring-2 " +
    "focus-visible:ring-violet focus-visible:ring-offset-2 focus-visible:ring-offset-canvas";
  const style = tone ? passStyle(tone) : undefined;
  const look = tone
    ? active
      ? `bg-(--p1) ring-1 ring-(--p1) shadow-[0_8px_18px_-10px_var(--p1)] ${tone === "yellow" ? "text-[#3b2f05]" : "text-white"}`
      : "bg-[color-mix(in_oklab,var(--p1)_9%,var(--surface))] text-[color-mix(in_oklab,var(--p1)_55%,var(--ink))] ring-1 ring-[color-mix(in_oklab,var(--p1)_45%,transparent)] hover:bg-[color-mix(in_oklab,var(--p1)_16%,var(--surface))]"
    : active
      ? "bg-accent text-on-accent ring-1 ring-accent shadow-[0_8px_18px_-10px_color-mix(in_oklab,var(--accent)_90%,transparent)]"
      : "bg-surface text-ink/80 ring-1 ring-hairline hover:text-ink hover:ring-accent/35";
  const dotColour = tone ? (active ? "currentColor" : "var(--p1)") : dot;
  return (
    <button type="button" onClick={onClick} aria-pressed={active} style={style} className={`${base} ${look}`}>
      {dotColour ? <span aria-hidden className="size-2 rounded-full" style={{ background: dotColour }} /> : null}
      <span>
        {children}
        {counted && count !== undefined ? <span className="tabular-nums"> ({count})</span> : null}
      </span>
    </button>
  );
}
