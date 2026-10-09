// Technician jobs (ARCHITECTURE.md §7.7): labels, the one next step, and day labels.
// A visit is booked for a date only; the technician phones the customer to agree the time.

import { ApiError, type Job, type JobStatus, authorizedFetch } from "@/lib/api";
import type { components } from "@/lib/api-types";

/** A job's pass colour (§11.3), by where it stands: waiting for the technician yellow, under way violet,
 * on the road orange, on site red, done green, cancelled slate. */
export const JOB_TONE: Record<JobStatus, "yellow" | "violet" | "orange" | "red" | "green" | "slate"> = {
  assigned: "yellow",
  accepted: "violet",
  en_route: "orange",
  on_site: "red",
  completed: "green",
  cancelled: "slate",
};

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  assigned: "Assigned",
  accepted: "Accepted",
  en_route: "On the way",
  on_site: "Arrived",
  completed: "Completed",
  cancelled: "Cancelled",
};

/** The technician's one next step from each status (§7.7 step 4). Completing asks for a note. */
export const NEXT_STEP: Partial<Record<JobStatus, { status: "accepted" | "en_route" | "on_site" | "completed"; label: string }>> = {
  assigned: { status: "accepted", label: "Accept" },
  accepted: { status: "en_route", label: "On the way" },
  en_route: { status: "on_site", label: "Arrived" },
  on_site: { status: "completed", label: "Complete" },
};

/** A rejection's reason: 3 to 300 characters (POST /api/jobs/{id}/reject). */
export const REJECT_REASON = { min: 3, max: 300 };

/** The assigned technician can't take this job: it closes, and another technician is found in the
 * background with the part still reserved (§7.7). Only while the job is "assigned". */
export async function rejectJob(id: string, reason: string): Promise<components["schemas"]["JobRejected"]> {
  const res = await authorizedFetch(`/api/jobs/${id}/reject`, { method: "POST", body: JSON.stringify({ reason }) });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new ApiError(res.status, typeof body?.detail === "string" ? body.detail : `Request failed (${res.status})`);
  }
  return res.json();
}

export function isOpen(job: Pick<Job, "status">): boolean {
  return job.status !== "completed" && job.status !== "cancelled";
}

/** Today's date as YYYY-MM-DD in the browser's time zone (the jobs are booked in IST dates). */
export function todayIso(now = new Date()): string {
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 10);
}

/** "Today", "Tomorrow", or "Wed 7 Oct". */
export function dayLabel(iso: string, now = new Date()): string {
  const today = todayIso(now);
  const tomorrow = todayIso(new Date(now.getTime() + 86_400_000));
  if (iso === today) return "Today";
  if (iso === tomorrow) return "Tomorrow";
  return new Date(`${iso}T00:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

/** The button text for a maps link, from the label the API gives it. */
export function mapsLinkText(label: string): string {
  if (label === "Google Maps") return "Open in Google Maps";
  if (label === "Apple Maps") return "Open in Apple Maps";
  return "Open the customer's map link";
}
