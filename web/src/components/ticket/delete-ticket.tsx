"use client";

import { LoaderCircle, Trash2 } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";
import { useStaffUser } from "@/lib/session";

/**
 * DELETE /api/tickets/{id} (section 10), for agents and admins: the ticket goes for good, with its
 * timeline, messages, payments and closed jobs. The server refuses while a technician job is open or
 * a payment is being verified, and that reason is shown here as is.
 */
export function DeleteTicket({ ticketId, ticketNumber }: { ticketId: string; ticketNumber: string }) {
  const role = useStaffUser()?.role;
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (role !== "agent" && role !== "admin") return null;

  async function remove() {
    setDeleting(true);
    setError(null);
    try {
      await api.deleteTicket(ticketId);
      router.replace("/inbox");
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Couldn’t delete this ticket.");
      setDeleting(false);
    }
  }

  if (!open) {
    return (
      <Button variant="ghost" size="sm" className="rounded-control text-danger hover:text-danger" onClick={() => setOpen(true)}>
        <Trash2 aria-hidden />
        Delete ticket
      </Button>
    );
  }

  return (
    <div role="group" aria-label="Delete this ticket" className="w-full space-y-2 rounded-card bg-danger/10 px-3 py-2.5">
      <p className="text-subheadline text-ink">
        Delete <span className="font-medium tabular-nums">{ticketNumber}</span> for good? Its timeline, messages,
        payments and closed jobs go with it. The customer is told by email and on their chat; they and their device
        stay. This can’t be undone.
      </p>
      {error ? (
        <p role="alert" className="text-footnote text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button variant="destructive" size="sm" className="rounded-control" disabled={deleting} onClick={() => void remove()}>
          {deleting ? <LoaderCircle className="animate-spin" aria-hidden /> : <Trash2 aria-hidden />}
          {deleting ? "Deleting…" : "Delete for good"}
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="rounded-control"
          disabled={deleting}
          onClick={() => {
            setOpen(false);
            setError(null);
          }}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
