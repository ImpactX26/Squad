"use client";

// One technician job (ARCHITECTURE.md §11.2, §7.7 step 3), mobile-first from 360 px: who to call,
// where to go (the address as text, a copy button, and its maps links; no map), the device, the
// issue, the part to carry, what was already tried, and whether it is paid or free under warranty.
// One big button moves the job to its next status (§7.7 step 4); Complete asks for a note. While the
// job is only assigned, Reject sits beside Accept and asks why. The backend decides who may move it:
// PATCH /api/jobs/{id} and POST /api/jobs/{id}/reject answer 403 for anyone but its technician.
// Refetched when /ws/staff reports a change to this job (§9).

import {
  ArrowLeft,
  Check,
  Copy,
  ExternalLink,
  LoaderCircle,
  Package,
  Phone,
} from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { ErrorState } from "@/components/states";
import { LiveBadge } from "@/components/ticket/live-badge";
import { Pass, PassBottom, PassBrand, PassFields, PassPill, PassTear, PassTop, passDate } from "@/components/ticket/pass";
import { Button } from "@/components/ui/button";
import { ApiError, type Job, api } from "@/lib/api";
import { dateOnly } from "@/lib/format";
import {
  JOB_STATUS_LABEL,
  JOB_TONE,
  NEXT_STEP,
  REJECT_REASON,
  dayLabel,
  mapsLinkText,
  rejectJob,
  todayIso,
} from "@/lib/jobs";
import { useStaffUser } from "@/lib/session";
import { type SocketStatus, openStaffSocket } from "@/lib/ws";

type Load =
  | { state: "loading" }
  | { state: "error"; message: string; notFound: boolean }
  | { state: "ready"; job: Job };

const JOB_EVENTS = new Set(["job.assigned", "job.status_changed", "job.completed", "job.rejected"]);

const TRIED_TONE: Record<string, string> = {
  worked: "text-success",
  failed: "text-danger",
  skipped: "text-ink-secondary",
};

export default function JobPage() {
  const { id } = useParams<{ id: string }>();
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++latest.current;
    try {
      const job = await api.job(id);
      if (mine === latest.current) setLoad({ state: "ready", job });
    } catch (err: unknown) {
      if (mine !== latest.current) return;
      setLoad((current) =>
        current.state === "ready"
          ? current
          : {
              state: "error",
              notFound: err instanceof ApiError && (err.status === 404 || err.status === 422 || err.status === 403),
              message: err instanceof Error ? err.message : "Couldn’t load this job.",
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
      if (!JOB_EVENTS.has(event.type) || event.data.job_id !== id) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void refresh(), 250);
    }, setSocket);
    return () => {
      if (timer) clearTimeout(timer);
      close();
    };
  }, [id, refresh]);

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div className="flex items-center gap-4">
        <Link
          href="/jobs"
          className="inline-flex items-center gap-1 rounded-control text-subheadline text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Jobs
        </Link>
        <LiveBadge status={socket} className="ml-auto" />
      </div>
      {load.state === "loading" ? <Skeleton /> : null}
      {load.state === "error" ? (
        <ErrorState
          message={load.notFound ? "This job doesn’t exist, or it isn’t yours." : load.message}
          onRetry={load.notFound ? undefined : () => void refresh()}
          action={
            load.notFound ? (
              <Button variant="outline" asChild className="rounded-control">
                <Link href="/jobs">Back to your jobs</Link>
              </Button>
            ) : undefined
          }
        />
      ) : null}
      {load.state === "ready" ? <JobView job={load.job} onChange={(job) => setLoad({ state: "ready", job })} /> : null}
    </div>
  );
}

function JobView({ job, onChange }: { job: Job; onChange: (job: Job) => void }) {
  const me = useStaffUser();
  const mine = me?.id === job.technician_id;
  const phoneHref = job.customer.phone ? `tel:${job.customer.phone.replace(/[^\d+]/g, "")}` : null;

  return (
    <>
      <header>
        <Pass tone={JOB_TONE[job.status]}>
          <PassTop tone={JOB_TONE[job.status]} className="px-6 pt-5 pb-6">
            <PassBrand right={passDate(job.scheduled_date)} />
            <div className="mt-4">
              <PassPill>{JOB_STATUS_LABEL[job.status]}</PassPill>
            </div>
            <h1 className="mt-3 text-[26px] leading-tight font-extrabold tracking-[-0.03em]">
              {job.service_name ?? job.service_code}
            </h1>
            <p className="text-[15px] font-semibold tabular-nums opacity-90">{job.ticket_number}</p>
          </PassTop>
          <PassTear />
          <PassBottom className="gap-3 px-6 pt-4 pb-5">
            <PassFields
              fields={[
                { label: "Visit", value: dayLabel(job.scheduled_date) },
                { label: "City", value: job.address.city },
                { label: "Billing", value: job.billing === "paid" ? "Paid" : "Warranty" },
              ]}
            />
            <p className="text-[14px] opacity-75">
              {dayLabel(job.scheduled_date)}, {dateOnly(job.scheduled_date)}. Phone the customer to agree the time.
            </p>
          </PassBottom>
        </Pass>
      </header>

      <Card title="Customer">
        <p className="text-body font-medium text-ink">{job.customer.full_name ?? "Customer"}</p>
        {phoneHref ? (
          <Button asChild className="mt-3 h-12 w-full rounded-control text-[17px]">
            <a href={phoneHref}>
              <Phone aria-hidden />
              Call {job.customer.phone}
            </a>
          </Button>
        ) : (
          <p className="text-subheadline text-ink-secondary">No phone number on record.</p>
        )}
      </Card>

      <Card title="Address">
        <p className="text-body text-ink">{job.address.text}</p>
        <CopyButton text={job.address.text} />
        <div className="mt-3 grid gap-2">
          {job.address.maps_links.map((link) => (
            <Button key={link.url} variant="outline" asChild className="h-11 w-full justify-between rounded-control text-[15px]">
              <a href={link.url} target="_blank" rel="noopener noreferrer">
                {mapsLinkText(link.label)}
                <ExternalLink aria-hidden />
              </a>
            </Button>
          ))}
        </div>
      </Card>

      <Card title="Device and issue">
        {job.device ? (
          <>
            <p className="text-body text-ink">{job.device.model_name}</p>
            <p className="text-subheadline text-ink-secondary">
              Serial <span className="tabular-nums text-ink">{job.device.serial_number}</span>
            </p>
          </>
        ) : (
          <p className="text-subheadline text-ink-secondary">No device recorded on the ticket.</p>
        )}
        <p className="mt-2 text-subheadline text-ink">{job.issue}</p>
      </Card>

      <Card title="Part to carry">
        {job.part ? (
          <div className="flex items-start gap-3">
            <Package className="mt-0.5 size-5 shrink-0 text-ink-secondary" aria-hidden />
            <div>
              <p className="text-body text-ink">
                <span className="font-medium tabular-nums">{job.part.sku}</span> · {job.part.name}
              </p>
              <p className="text-subheadline text-ink-secondary">
                Reserved at {job.part.warehouse_name ?? "the warehouse"}
              </p>
            </div>
          </div>
        ) : (
          <p className="text-subheadline text-ink-secondary">No part needed for this job.</p>
        )}
      </Card>

      <Card title="Already tried">
        {job.tried.length ? (
          <ul className="space-y-1.5">
            {job.tried.map((step, index) => (
              <li key={index} className="flex gap-2 text-subheadline">
                <span className={`shrink-0 font-medium ${TRIED_TONE[step.result] ?? "text-ink-secondary"}`}>
                  {step.result === "worked" ? "Worked" : step.result === "failed" ? "Didn’t help" : "Skipped"}
                </span>
                <span className="text-ink">{step.step}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-subheadline text-ink-secondary">Nothing recorded yet.</p>
        )}
      </Card>

      <Card title="Billing">
        <p className="text-body text-ink">
          {job.billing === "paid" ? (
            <>
              Paid{job.invoice_number ? <span className="tabular-nums">, {job.invoice_number}</span> : null}
            </>
          ) : (
            "Free under warranty"
          )}
        </p>
        {job.device?.warranty_until ? (
          <p className="text-subheadline text-ink-secondary">
            {job.device.warranty_until >= todayIso() ? "Warranty until" : "Warranty ended"}{" "}
            {dateOnly(job.device.warranty_until)}
          </p>
        ) : null}
      </Card>

      {job.notes ? (
        <Card title="Notes">
          <p className="whitespace-pre-wrap text-subheadline text-ink">{job.notes}</p>
        </Card>
      ) : null}

      <NextStep job={job} mine={mine} technician={job.technician_name} onChange={onChange} />
    </>
  );
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="rounded-card bg-surface p-5 shadow-card">
      <h2 className="mb-2 text-[15px] font-bold text-ink">{title}</h2>
      {children}
    </section>
  );
}

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    setTimeout(() => setState("idle"), 2000);
  }
  return (
    <div className="mt-2 flex items-center gap-2">
      <Button variant="ghost" onClick={() => void copy()} className="h-9 rounded-control px-2 text-[15px] text-accent">
        {state === "copied" ? <Check aria-hidden /> : <Copy aria-hidden />}
        {state === "copied" ? "Copied" : "Copy address"}
      </Button>
      {state === "failed" ? (
        <span role="alert" className="text-footnote text-danger">
          Couldn’t copy. Select the address and copy it.
        </span>
      ) : null}
    </div>
  );
}

/** The one big button for the next status, pinned to the bottom of the screen on a phone. */
function NextStep({
  job,
  mine,
  technician,
  onChange,
}: {
  job: Job;
  mine: boolean;
  technician: string | null;
  onChange: (job: Job) => void;
}) {
  const next = NEXT_STEP[job.status];
  const router = useRouter();
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [rejected, setRejected] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function reject() {
    setBusy(true);
    setError(null);
    try {
      await rejectJob(job.id, reason.trim());
      setRejected(true);
      setTimeout(() => router.push("/jobs"), 1800);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Couldn’t reject the job.");
    } finally {
      setBusy(false);
    }
  }

  async function move(status: NonNullable<typeof next>["status"], notes?: string) {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.patchJob(job.id, { status, notes: notes ?? null }));
      setNoting(false);
      setNote("");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Couldn’t update the job.");
    } finally {
      setBusy(false);
    }
  }

  let content: ReactNode;
  if (rejected) {
    content = (
      <p role="status" className="py-2 text-center text-subheadline text-ink">
        You rejected this job. We’re finding another technician.
      </p>
    );
  } else if (rejecting && mine && job.status === "assigned") {
    const length = reason.trim().length;
    const ok = length >= REJECT_REASON.min && length <= REJECT_REASON.max;
    content = (
      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (ok) void reject();
        }}
      >
        <label htmlFor="reject-reason" className="text-subheadline font-medium text-ink">
          Why can’t you take this job?
        </label>
        <textarea
          id="reject-reason"
          autoFocus
          rows={3}
          maxLength={REJECT_REASON.max}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          placeholder="e.g. I’m off sick that day"
          className="w-full resize-y rounded-control bg-surface px-3 py-2 text-body text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent"
        />
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={() => setRejecting(false)} disabled={busy} className="h-12 rounded-control text-[17px]">
            Back
          </Button>
          <Button type="submit" disabled={!ok || busy} className="h-12 flex-1 rounded-control bg-danger text-[17px] hover:bg-danger/90">
            {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
            Reject job
          </Button>
        </div>
      </form>
    );
  } else if (!next) {
    content = (
      <p className="py-2 text-center text-subheadline text-ink-secondary">
        {job.status === "completed"
          ? `Completed${job.completed_at ? ` ${new Date(job.completed_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}` : ""}.`
          : "This job was cancelled."}
      </p>
    );
  } else if (!mine) {
    content = (
      <p className="py-2 text-center text-subheadline text-ink-secondary">
        Only {technician ?? "the assigned technician"} can update this job.
      </p>
    );
  } else if (next.status === "completed" && noting) {
    const ok = note.trim().length >= 3;
    content = (
      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (ok) void move("completed", note.trim());
        }}
      >
        <label htmlFor="completion-note" className="text-subheadline font-medium text-ink">
          What did you do?
        </label>
        <textarea
          id="completion-note"
          autoFocus
          rows={3}
          value={note}
          onChange={(event) => setNote(event.target.value)}
          placeholder="e.g. Replaced the battery; it charges to 100%"
          className="w-full resize-y rounded-control bg-surface px-3 py-2 text-body text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent"
        />
        <div className="flex gap-2">
          <Button type="button" variant="outline" onClick={() => setNoting(false)} disabled={busy} className="h-12 rounded-control text-[17px]">
            Back
          </Button>
          <Button type="submit" disabled={!ok || busy} className="h-12 flex-1 rounded-control text-[17px]">
            {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : <Check aria-hidden />}
            Complete job
          </Button>
        </div>
      </form>
    );
  } else {
    const primary = (
      <Button
        onClick={() => (next.status === "completed" ? setNoting(true) : void move(next.status))}
        disabled={busy}
        className="h-14 w-full flex-1 rounded-control text-[17px] font-semibold"
      >
        {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
        {next.label}
      </Button>
    );
    // Reject only while the job is assigned: an accepted job is committed, and only an admin cancels it.
    content =
      job.status === "assigned" ? (
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            onClick={() => {
              setError(null);
              setRejecting(true);
            }}
            disabled={busy}
            className="h-14 rounded-control px-5 text-[17px]"
          >
            Reject
          </Button>
          {primary}
        </div>
      ) : (
        primary
      );
  }

  return (
    <div className="sticky bottom-0 -mx-4 border-t border-hairline bg-canvas/90 px-4 pt-3 pb-[max(0.75rem,env(safe-area-inset-bottom))] backdrop-blur-[20px]">
      {content}
      {error ? (
        <p role="alert" className="mt-2 text-center text-footnote text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function Skeleton() {
  return (
    <div className="space-y-3" aria-busy="true" aria-label="Loading job">
      <div className="h-16 rounded-card bg-surface" />
      <div className="h-28 rounded-card bg-surface" />
      <div className="h-36 rounded-card bg-surface" />
      <div className="h-24 rounded-card bg-surface" />
    </div>
  );
}
