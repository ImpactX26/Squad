"use client";

// The agent's ticket page (ARCHITECTURE.md sections 11.2 and 11.3): title bar, AI summary, the
// unified timeline across every channel, the composer, and a right rail. It reads
// GET /api/tickets/{id} and /timeline, and refetches when /ws/staff reports a change to this
// ticket (section 9), so a customer reply or a teammate's edit shows up without a reload.

import { ArrowLeft, Bot } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ToolCall } from "@/components/activity-rail/activity-rail";
import { Composer } from "@/components/composer/composer";
import { ErrorState } from "@/components/states";
import { DeleteTicket } from "@/components/ticket/delete-ticket";
import { ChannelStack } from "@/components/ticket/glyphs";
import { LiveBadge } from "@/components/ticket/live-badge";
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
import { RightRail } from "@/components/ticket/right-rail";
import { Timeline } from "@/components/timeline/timeline";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  type DiagnosticStep,
  type SourceChannel,
  type TicketDetail,
  type TicketPriority,
  type TicketStatus,
  type Timeline as TimelineData,
  api,
} from "@/lib/api";
import { CHANNEL_LABEL, FLAG_LABEL, PRIORITY_LABEL, STATUS_LABEL, relative } from "@/lib/format";
import { type SocketStatus, type StaffEvent, openStaffSocket } from "@/lib/ws";

// Events that can change what this page shows, when they carry this ticket id (section 9).
const LIVE_EVENTS = new Set<StaffEvent["type"]>([
  "ticket.created",
  "ticket.updated",
  "ticket.followup",
  "message.received",
  "message.sent",
  "payment.link_sent",
  "payment.paid",
  "payment.failed",
  "job.assigned",
  "job.status_changed",
  "job.completed",
]);

const STATUSES = Object.keys(STATUS_LABEL) as TicketStatus[];
// The Agent Activity rail keeps the latest calls; older ones are in ai_runs.
const MAX_TOOL_CALLS = 40;
const PRIORITIES = ["urgent", "high", "medium", "low"] as TicketPriority[];

const SELECT =
  "rounded-control bg-canvas py-1 pl-2.5 pr-2 text-subheadline text-ink outline-none " +
  "focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 dark:bg-surface-raised";

type Load =
  | { state: "loading" }
  | { state: "error"; message: string; notFound: boolean }
  | { state: "ready"; ticket: TicketDetail; timeline: TimelineData };

export default function TicketPage() {
  const { id } = useParams<{ id: string }>();
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const [editError, setEditError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [toolCalls, setToolCalls] = useState<ToolCall[]>([]);
  // Bumped with every live change, so the suggested chips are picked again (cached server-side).
  const [suggestionsKey, setSuggestionsKey] = useState(0);
  // Only the newest request may write state, so a slow older response never overwrites a newer one.
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++latest.current;
    try {
      const [ticket, timeline] = await Promise.all([api.ticket(id), api.timeline(id)]);
      if (mine === latest.current) setLoad({ state: "ready", ticket, timeline });
    } catch (err: unknown) {
      if (mine !== latest.current) return;
      // A failed live refresh keeps showing what is already on screen.
      setLoad((current) =>
        current.state === "ready"
          ? current
          : {
              state: "error",
              notFound: err instanceof ApiError && (err.status === 404 || err.status === 422),
              message: err instanceof Error ? err.message : "Couldn’t load this ticket.",
            },
      );
    }
  }, [id]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const close = openStaffSocket((event) => {
      if (event.data.ticket_id !== id) return;
      if (event.type === "agent.tool_called") {
        const d = event.data;
        const call: ToolCall = {
          key: `${event.ts}-${String(d.tool)}-${Math.random().toString(36).slice(2, 8)}`,
          tool: String(d.tool ?? "tool"),
          ok: d.ok !== false,
          ms: typeof d.ms === "number" ? d.ms : 0,
          trigger: String(d.trigger ?? ""),
          role: String(d.role ?? ""),
          at: event.ts,
        };
        setToolCalls((current) => [...current, call].slice(-MAX_TOOL_CALLS));
        return;
      }
      if (!LIVE_EVENTS.has(event.type)) return;
      // One customer message raises several events in a burst; fetch once for the lot.
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        void refresh();
        setSuggestionsKey((n) => n + 1);
      }, 250);
    }, setSocket);
    return () => {
      if (timer) clearTimeout(timer);
      close();
    };
  }, [id, refresh]);

  const channels: SourceChannel[] = useMemo(() => {
    if (load.state !== "ready") return [];
    const seen = load.timeline.channels.filter((c): c is SourceChannel => c !== "internal");
    return seen.length ? seen : load.ticket.channels;
  }, [load]);

  const replyChannel = useMemo<SourceChannel | null>(() => {
    if (load.state !== "ready") return null;
    const last = [...load.timeline.entries]
      .reverse()
      .find((e) => e.kind === "message" && !e.is_internal_note && e.channel && e.channel !== "internal");
    return (last?.channel as SourceChannel | undefined) ?? load.ticket.source_channel;
  }, [load]);

  async function edit(patch: { status?: TicketStatus; priority?: TicketPriority }) {
    setSaving(true);
    setEditError(null);
    try {
      await api.patchTicket(id, patch);
      await refresh();
    } catch (err: unknown) {
      setEditError(err instanceof Error ? err.message : "Couldn’t save that change.");
    } finally {
      setSaving(false);
    }
  }

  function onStepChange(step: DiagnosticStep) {
    setLoad((current) =>
      current.state === "ready"
        ? {
            ...current,
            ticket: {
              ...current.ticket,
              diagnostic_steps: current.ticket.diagnostic_steps.map((s) => (s.id === step.id ? step : s)),
            },
          }
        : current,
    );
  }

  if (load.state === "loading") return <PageSkeleton />;

  if (load.state === "error") {
    return (
      <div className="space-y-4">
        <BackLink />
        <ErrorState
          message={load.notFound ? "This ticket doesn’t exist, or it was removed." : load.message}
          onRetry={load.notFound ? undefined : () => void refresh()}
          action={
            load.notFound ? (
              <Button variant="outline" asChild className="rounded-control">
                <Link href="/inbox">Back to the inbox</Link>
              </Button>
            ) : undefined
          }
        />
      </div>
    );
  }

  const { ticket, timeline } = load;
  const customerName = ticket.customer?.full_name ?? "Customer";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <BackLink />
        <LiveBadge status={socket} className="ml-auto" />
        <DeleteTicket ticketId={ticket.id} ticketNumber={ticket.ticket_number} />
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="min-w-0 space-y-6">
          <header className="space-y-3">
            {/* The ticket as a pass (§11.3), in its priority's colour; the controls to change it sit below. */}
            <Pass tone={PRIORITY_TONE[ticket.priority]}>
              <PassTop tone={PRIORITY_TONE[ticket.priority]} className="px-6 pt-5 pb-6">
                <PassBrand right={passDate(ticket.updated_at)} />
                <div className="mt-4">
                  <PassPill>{PRIORITY_LABEL[ticket.priority]}</PassPill>
                </div>
                <p className="mt-3 text-[16px] font-semibold">
                  {ticket.product ? ticket.product.model_name : "No verified device"}
                </p>
                <p className="text-[30px] leading-tight font-extrabold tracking-[-0.03em] tabular-nums">
                  {ticket.ticket_number}
                </p>
              </PassTop>
              <PassTear />
              <PassBottom className="gap-3 px-6 pt-4 pb-5">
                <h1 className="text-[24px] leading-tight font-bold tracking-[-0.02em]">{ticket.title}</h1>
                <PassFields
                  fields={[
                    { label: "Channel", value: CHANNEL_LABEL[ticket.source_channel] },
                    { label: "Type", value: sentence(ticket.category) },
                    { label: "Status", value: STATUS_LABEL[ticket.status] },
                  ]}
                />
                <PassPerson
                  name={ticket.customer?.full_name}
                  trailing={<time dateTime={ticket.updated_at}>{relative(ticket.updated_at)}</time>}
                />
              </PassBottom>
            </Pass>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
              <ChannelStack channels={channels} />
              <label className="inline-flex items-center gap-2 text-footnote text-ink-secondary">
                Priority
                <select
                  className={SELECT}
                  value={ticket.priority}
                  disabled={saving}
                  onChange={(e) => void edit({ priority: e.target.value as TicketPriority })}
                >
                  {PRIORITIES.map((p) => (
                    <option key={p} value={p}>
                      {PRIORITY_LABEL[p]}
                    </option>
                  ))}
                </select>
              </label>
              <label className="inline-flex items-center gap-2 text-footnote text-ink-secondary">
                Status
                <select
                  className={SELECT}
                  value={ticket.status}
                  disabled={saving}
                  onChange={(e) => void edit({ status: e.target.value as TicketStatus })}
                >
                  {STATUSES.map((s) => (
                    <option key={s} value={s}>
                      {STATUS_LABEL[s]}
                    </option>
                  ))}
                </select>
              </label>
              {ticket.duplicate_count > 0 ? (
                <span className="text-footnote font-medium tabular-nums text-ink-secondary">
                  +{ticket.duplicate_count} follow-up{ticket.duplicate_count === 1 ? "" : "s"}
                </span>
              ) : null}
            </div>
            {ticket.flags.length ? (
              <div className="flex flex-wrap gap-2">
                {ticket.flags.map((flag) => (
                  <span key={flag} className="rounded-full bg-warning/15 px-2.5 py-0.5 text-footnote text-warning">
                    {FLAG_LABEL[flag] ?? flag}
                  </span>
                ))}
              </div>
            ) : null}
            {editError ? (
              <p role="alert" className="text-footnote text-danger">
                {editError}
              </p>
            ) : null}
          </header>

          <section aria-label="Summary" className="space-y-1.5">
            <h2 className="flex items-center gap-2 text-title-2 font-semibold tracking-tight text-ink">
              Summary
              {ticket.ai_summary ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-accent/12 px-2 py-0.5 text-footnote font-medium text-accent">
                  <Bot className="size-3" aria-hidden /> AI
                </span>
              ) : null}
            </h2>
            {ticket.ai_summary ? (
              <p className="text-body text-ink">{ticket.ai_summary}</p>
            ) : (
              <p className="text-subheadline text-ink-secondary">
                No AI summary yet.{" "}
                {ticket.description ? <span className="text-ink">{ticket.description}</span> : null}
              </p>
            )}
          </section>

          <section aria-label="Conversation" className="space-y-3">
            <h2 className="text-title-2 font-semibold tracking-tight text-ink">Timeline</h2>
            <Timeline entries={timeline.entries} customerName={customerName} />
          </section>

          <Composer
            ticketId={ticket.id}
            replyChannel={replyChannel}
            onSent={() => void refresh()}
            suggestionsKey={suggestionsKey}
          />
        </div>

        <aside aria-label="Ticket details" className="min-w-0">
          <RightRail
            ticket={ticket}
            toolCalls={toolCalls}
            channels={channels}
            onStepChange={onStepChange}
            onPaymentChange={() => void refresh()}
          />
        </aside>
      </div>
    </div>
  );
}

const sentence = (value: string) => value.charAt(0).toUpperCase() + value.slice(1);

function BackLink() {
  return (
    <Link
      href="/inbox"
      className="inline-flex items-center gap-1 rounded-control text-subheadline text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
    >
      <ArrowLeft className="size-4" aria-hidden />
      Inbox
    </Link>
  );
}

function PageSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading ticket">
      <div className="h-5 w-16 rounded-control bg-surface" />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="space-y-4">
          <div className="h-9 w-2/3 rounded-control bg-surface" />
          <div className="h-24 rounded-card bg-surface" />
          <div className="h-48 rounded-card bg-surface" />
        </div>
        <div className="space-y-3">
          <div className="h-24 rounded-card bg-surface" />
          <div className="h-24 rounded-card bg-surface" />
          <div className="h-40 rounded-card bg-surface" />
        </div>
      </div>
    </div>
  );
}
