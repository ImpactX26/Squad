"use client";

import { type FormEvent, useId, useState } from "react";

import { Button } from "@/components/ui/button";
import { api } from "@/lib/api";

const MIN_NOTE = 3;

/**
 * An admin's backup when a bank alert never arrives (section 7.6): POST /api/payments/{id}/mark-paid
 * with a note saying what they checked. The server checks the role again and records the admin's id;
 * payment.paid then sends the receipt as usual.
 */
export function MarkPaid({ paymentId, onPaid }: { paymentId: string; onPaid: () => void }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteId = useId();
  const ready = note.trim().length >= MIN_NOTE;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setSaving(true);
    setError(null);
    try {
      await api.markPaid(paymentId, note.trim());
      setOpen(false);
      setNote("");
      onPaid();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Couldn’t mark this payment paid.");
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <Button variant="outline" size="sm" className="mt-3 rounded-control" onClick={() => setOpen(true)}>
        Mark as paid
      </Button>
    );
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="mt-3 space-y-2">
      <label htmlFor={noteId} className="block text-footnote text-ink-secondary">
        What did you check? This note is kept with your name.
      </label>
      <textarea
        id={noteId}
        value={note}
        onChange={(e) => setNote(e.target.value)}
        rows={2}
        maxLength={500}
        required
        placeholder="UTR seen on the bank statement"
        className="w-full resize-y rounded-control bg-canvas px-2.5 py-2 text-subheadline text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised"
      />
      {error ? (
        <p role="alert" className="text-footnote text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button type="submit" size="sm" className="rounded-control" disabled={!ready || saving}>
          {saving ? "Marking…" : "Confirm paid"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="rounded-control"
          disabled={saving}
          onClick={() => {
            setOpen(false);
            setError(null);
          }}
        >
          Cancel
        </Button>
      </div>
    </form>
  );
}
