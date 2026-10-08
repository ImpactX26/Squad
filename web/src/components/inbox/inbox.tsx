"use client";

import { Inbox as InboxIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { useInboxTickets } from "@/components/inbox/data";
import { TicketList } from "@/components/inbox/ticket-list";
import { TicketPreview } from "@/components/inbox/ticket-preview";
import { ALL_VIEWS, CHANNEL_VIEWS, VIEWS, type View } from "@/components/inbox/views";
import { EmptyState, ErrorState, LoadingRows } from "@/components/states";
import { CHANNELS } from "@/components/ticket/channel";
import { useSession } from "@/lib/auth";
import { useNow } from "@/lib/use-now";
import { cn } from "@/lib/utils";

/**
 * The inbox (§11.2): views sidebar, ticket list, ticket preview. Below md one pane at a time;
 * md to lg the views become chips above the list.
 */
export function Inbox() {
  const session = useSession();
  const me = session?.staff.id ?? "";
  const now = useNow();
  const [viewId, setViewId] = useState("open");
  const [picked, setPicked] = useState<string | null>(null);
  const [mobilePreview, setMobilePreview] = useState(false);

  const view = ALL_VIEWS.find((v) => v.id === viewId) ?? VIEWS[0];
  const { state, reload } = useInboxTickets(view.query, me);
  const tickets = useMemo(() => (state.status === "ready" ? state.tickets : []), [state]);
  // The picked ticket, or the first one: the preview is never empty while the list isn't.
  const selected = tickets.find((t) => t.id === picked) ?? tickets[0] ?? null;

  function chooseView(id: string) {
    setViewId(id);
    setPicked(null);
    setMobilePreview(false);
  }

  return (
    <div className="flex h-[calc(100dvh-3.5rem)] gap-3 p-2 sm:p-3">
      <aside aria-label="Views" className="hidden w-56 shrink-0 overflow-y-auto py-2 pl-1 lg:block">
        <ViewGroup views={VIEWS} current={view.id} onChoose={chooseView} />
        <p className="mt-6 mb-1.5 px-3 text-footnote text-ink-secondary">Channels</p>
        <ViewGroup views={CHANNEL_VIEWS} current={view.id} onChoose={chooseView} />
      </aside>

      <section
        aria-label="Ticket list"
        className={cn(
          "min-w-0 flex-1 flex-col overflow-hidden rounded-panel bg-surface md:w-[22rem] md:flex-none lg:w-[24rem] xl:w-[26rem]",
          mobilePreview ? "hidden md:flex" : "flex",
        )}
      >
        <div className="px-4 pt-4 pb-2">
          <div className="flex items-baseline justify-between gap-3">
            <h1 className="text-title-2 font-semibold">{view.label}</h1>
            {state.status === "ready" && (
              <span className="text-footnote text-ink-secondary tabular-nums">
                {state.total} ticket{state.total === 1 ? "" : "s"}
              </span>
            )}
          </div>
          <ViewChips current={view.id} onChoose={chooseView} />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {state.status === "loading" && <LoadingRows />}
          {state.status === "error" && <ErrorState message={state.message} onRetry={reload} />}
          {state.status === "ready" && tickets.length === 0 && (
            <EmptyState icon={view.channel ? CHANNELS[view.channel].Icon : InboxIcon} title="No tickets here">
              {view.id === "open" ? "New tickets appear as customers write in." : "Nothing matches this view right now."}
            </EmptyState>
          )}
          {state.status === "ready" && tickets.length > 0 && (
            <TicketList
              tickets={tickets}
              selectedId={selected?.id ?? null}
              now={now}
              onSelect={(id) => {
                setPicked(id);
                setMobilePreview(true);
              }}
            />
          )}
        </div>
      </section>

      <section
        aria-label="Ticket preview"
        className={cn("min-w-0 flex-1 overflow-y-auto rounded-panel bg-surface", mobilePreview ? "block" : "hidden md:block")}
      >
        {selected ? (
          <TicketPreview ticket={selected} me={me} now={now} onBack={() => setMobilePreview(false)} />
        ) : (
          state.status === "ready" && (
            <EmptyState title="No ticket selected" className="h-full">
              Pick a ticket to see its summary and conversation.
            </EmptyState>
          )
        )}
      </section>
    </div>
  );
}

function ViewGroup({ views, current, onChoose }: { views: View[]; current: string; onChoose: (id: string) => void }) {
  return (
    <ul className="space-y-0.5">
      {views.map((v) => {
        const active = v.id === current;
        const channel = v.channel ? CHANNELS[v.channel] : null;
        const Icon = channel?.Icon ?? v.Icon;
        return (
          <li key={v.id}>
            <button
              type="button"
              aria-current={active ? "true" : undefined}
              onClick={() => onChoose(v.id)}
              className={cn(
                "flex h-9 w-full items-center gap-2.5 rounded-control px-3 text-left text-subheadline transition-colors",
                active ? "bg-surface font-semibold text-ink" : "text-ink-secondary hover:bg-surface/60 hover:text-ink",
              )}
            >
              {Icon && <Icon aria-hidden="true" className={cn("size-4 shrink-0", channel?.text)} />}
              {v.label}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** The views as a scrolling row of chips, below lg. */
function ViewChips({ current, onChoose }: { current: string; onChoose: (id: string) => void }) {
  return (
    <ul aria-label="Views" className="-mx-4 mt-3 flex gap-1.5 overflow-x-auto px-4 pb-1 lg:hidden">
      {ALL_VIEWS.map((v) => (
        <li key={v.id} className="shrink-0">
          <button
            type="button"
            aria-current={v.id === current ? "true" : undefined}
            onClick={() => onChoose(v.id)}
            className={cn(
              "h-8 rounded-full px-3 text-footnote font-semibold whitespace-nowrap transition-colors",
              v.id === current ? "bg-ink text-canvas" : "bg-canvas text-ink-secondary hover:text-ink",
            )}
          >
            {v.label}
          </button>
        </li>
      ))}
    </ul>
  );
}
