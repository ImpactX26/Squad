"use client";

// The technician's jobs (ARCHITECTURE.md §11.2, §7.7), mobile-first: today's, then upcoming, then
// any still open from an earlier day. GET /api/jobs/mine; refetched when /ws/staff reports a job of
// this technician's changing (§9), so a new booking appears without a reload. No maps: the city is
// text, and the job page has the address and its links.

import { ChevronRight, Package } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";

import { EmptyState, ErrorState, LoadingState } from "@/components/states";
import { LiveBadge } from "@/components/ticket/live-badge";
import { Pass, PassBottom, PassBrand, PassFields, PassPerson, PassPill, PassTear, PassTop } from "@/components/ticket/pass";
import { type Job, api } from "@/lib/api";
import { JOB_STATUS_LABEL, JOB_TONE, dayLabel, isOpen, todayIso } from "@/lib/jobs";
import { useStaffUser } from "@/lib/session";
import { type SocketStatus, openStaffSocket } from "@/lib/ws";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; jobs: Job[] };

const JOB_EVENTS = new Set(["job.assigned", "job.status_changed", "job.completed", "job.rejected"]);

export default function JobsPage() {
  const me = useStaffUser();
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const latest = useRef(0);

  const refresh = useCallback(async () => {
    const mine = ++latest.current;
    try {
      const { jobs } = await api.myJobs();
      if (mine === latest.current) setLoad({ state: "ready", jobs });
    } catch (err: unknown) {
      if (mine !== latest.current) return;
      setLoad((current) =>
        current.state === "ready"
          ? current
          : { state: "error", message: err instanceof Error ? err.message : "Couldn’t load your jobs." },
      );
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const close = openStaffSocket((event) => {
      if (!JOB_EVENTS.has(event.type) || (me && event.data.technician_id !== me.id)) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void refresh(), 250);
    }, setSocket);
    return () => {
      if (timer) clearTimeout(timer);
      close();
    };
  }, [me, refresh]);

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <div className="flex items-end gap-3">
        <h1 className="text-large-title font-bold text-ink">Jobs</h1>
        <LiveBadge status={socket} className="mb-1 ml-auto" />
      </div>
      <Body load={load} retry={() => void refresh()} />
    </div>
  );
}

function Body({ load, retry }: { load: Load; retry: () => void }) {
  if (load.state === "loading") return <LoadingState label="Loading jobs" rowClassName="h-56 rounded-[26px]" />;
  if (load.state === "error") return <ErrorState message={load.message} onRetry={retry} />;

  const today = todayIso();
  const groups: { title: string; jobs: Job[] }[] = [
    { title: "Today", jobs: load.jobs.filter((j) => j.scheduled_date === today) },
    { title: "Upcoming", jobs: load.jobs.filter((j) => j.scheduled_date > today) },
    { title: "Earlier, still open", jobs: load.jobs.filter((j) => j.scheduled_date < today && isOpen(j)) },
  ].filter((g) => g.jobs.length);

  if (!groups.length) {
    return (
      <EmptyState
        icon={Package}
        title="No jobs for you today or coming up."
        hint="New visits appear here as soon as they are booked."
      />
    );
  }

  return (
    <div className="space-y-6">
      {groups.map((group) => (
        <section key={group.title} aria-label={group.title} className="space-y-3">
          <h2 className="text-[17px] font-bold text-ink">{group.title}</h2>
          <ul className="grid gap-4 sm:grid-cols-2">
            {group.jobs.map((job) => (
              <JobPass key={job.id} job={job} />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

/** One job as a pass (§11.3), coloured by where it stands: the service, the ticket and the day on top; the
 * city, device and billing, and the customer, below. Opens the job. */
function JobPass({ job }: { job: Job }) {
  const tone = JOB_TONE[job.status];
  return (
    <li>
      <Link
        href={`/jobs/${job.id}`}
        prefetch={false}
        aria-label={`${job.service_name ?? job.service_code} for ${job.customer.full_name ?? "a customer"}, ${dayLabel(job.scheduled_date)}`}
        className="group block h-full rounded-[26px] outline-none focus-visible:ring-[3px] focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-canvas"
      >
        <Pass
          tone={tone}
          className="h-full transition-[transform,box-shadow] duration-200 ease-out group-hover:-translate-y-1 group-hover:shadow-[0_24px_40px_-22px_color-mix(in_oklab,var(--p1)_85%,transparent)] group-active:translate-y-0"
        >
          <PassTop tone={tone} className="px-5 pt-4 pb-5">
            <PassBrand right={dayLabel(job.scheduled_date)} />
            <div className="mt-4">
              <PassPill>{JOB_STATUS_LABEL[job.status]}</PassPill>
            </div>
            <p className="mt-3 truncate text-[16px] font-semibold">{job.service_name ?? job.service_code}</p>
            <p className="text-[24px] leading-tight font-extrabold tracking-[-0.03em] tabular-nums">{job.ticket_number}</p>
          </PassTop>
          <PassTear />
          <PassBottom className="gap-3 px-5 pt-4 pb-4">
            <PassFields
              fields={[
                { label: "City", value: job.address.city },
                { label: "Device", value: <span title={job.device?.model_name}>{job.device ? job.device.model_name : "Not recorded"}</span> },
                { label: "Billing", value: job.billing === "paid" ? "Paid" : "Warranty" },
              ]}
            />
            <div className="mt-auto">
              <PassPerson
                name={job.customer.full_name}
                trailing={<ChevronRight className="size-4 transition-transform group-hover:translate-x-0.5" aria-hidden />}
              />
            </div>
          </PassBottom>
        </Pass>
      </Link>
    </li>
  );
}
