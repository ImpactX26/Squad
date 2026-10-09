"use client";

import { Bot, Check, CircleDashed, SkipForward, X } from "lucide-react";
import { useState } from "react";

import { type DiagnosticResult, type DiagnosticStep, api } from "@/lib/api";

const RESULTS: { value: Exclude<DiagnosticResult, "pending">; label: string; icon: typeof Check; tint: string }[] = [
  { value: "worked", label: "Worked", icon: Check, tint: "bg-success/15 text-success" },
  { value: "failed", label: "Failed", icon: X, tint: "bg-danger/15 text-danger" },
  { value: "skipped", label: "Skipped", icon: SkipForward, tint: "bg-canvas text-ink dark:bg-surface-raised" },
];

const SOURCE_LABEL: Record<DiagnosticStep["suggested_by"], string> = {
  ai: "AI",
  agent: "Agent",
  playbook: "Playbook",
};

/**
 * The "what was tried, what worked" checklist (section 5.1). Each press is saved with
 * PATCH /api/tickets/{id}/diagnostics/{step_id}; the row updates at once and rolls back with a
 * message if the save fails. Pressing the active result again puts the step back to pending.
 */
export function DiagnosticsPanel({
  ticketId,
  steps,
  onStepChange,
}: {
  ticketId: string;
  steps: DiagnosticStep[];
  onStepChange: (step: DiagnosticStep) => void;
}) {
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<Set<string>>(new Set());

  async function choose(step: DiagnosticStep, value: Exclude<DiagnosticResult, "pending">) {
    const next: DiagnosticResult = step.result === value ? "pending" : value;
    setError(null);
    setSaving((current) => new Set(current).add(step.id));
    onStepChange({ ...step, result: next });
    try {
      onStepChange(await api.patchDiagnostic(ticketId, step.id, next));
    } catch (err: unknown) {
      onStepChange(step);
      setError(err instanceof Error ? err.message : "Couldn’t save that step.");
    } finally {
      setSaving((current) => {
        const copy = new Set(current);
        copy.delete(step.id);
        return copy;
      });
    }
  }

  if (steps.length === 0) {
    return (
      <p className="text-footnote text-ink-secondary">
        No diagnostic steps on this ticket yet. Type /diagnose in the composer to add the playbook’s.
      </p>
    );
  }

  return (
    <div className="space-y-2">
      <ul className="space-y-3">
        {steps.map((step) => (
          <li key={step.id} className="space-y-1.5">
            <div className="flex items-start gap-2">
              <StepMark result={step.result} />
              <p className={`min-w-0 flex-1 text-subheadline ${step.result === "skipped" ? "text-ink-secondary" : "text-ink"}`}>
                {step.step}
              </p>
              <span
                className={
                  step.suggested_by === "ai"
                    ? "inline-flex shrink-0 items-center gap-1 rounded-full bg-accent/12 px-2 py-0.5 text-footnote font-medium text-accent"
                    : "shrink-0 rounded-full bg-canvas px-2 py-0.5 text-footnote text-ink-secondary dark:bg-surface-raised"
                }
                title={step.suggested_by === "ai" ? "Suggested by the AI" : `Added by ${SOURCE_LABEL[step.suggested_by].toLowerCase()}`}
              >
                {step.suggested_by === "ai" ? <Bot className="size-3" aria-hidden /> : null}
                {SOURCE_LABEL[step.suggested_by]}
              </span>
            </div>
            <div role="group" aria-label={`Result for: ${step.step}`} className="flex gap-1.5 pl-6">
              {RESULTS.map(({ value, label, icon: Icon, tint }) => {
                const active = step.result === value;
                return (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={active}
                    disabled={saving.has(step.id)}
                    onClick={() => void choose(step, value)}
                    className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-footnote outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 ${
                      active ? `${tint} font-medium` : "bg-canvas text-ink-secondary hover:text-ink dark:bg-surface-raised"
                    }`}
                  >
                    <Icon className="size-3" aria-hidden />
                    {label}
                  </button>
                );
              })}
            </div>
          </li>
        ))}
      </ul>
      {error ? (
        <p role="alert" className="text-footnote text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}

function StepMark({ result }: { result: DiagnosticResult }) {
  const common = "mt-0.5 size-4 shrink-0";
  switch (result) {
    case "worked":
      return <Check className={`${common} text-success`} aria-label="Worked" role="img" />;
    case "failed":
      return <X className={`${common} text-danger`} aria-label="Failed" role="img" />;
    case "skipped":
      return <SkipForward className={`${common} text-ink-secondary`} aria-label="Skipped" role="img" />;
    default:
      return <CircleDashed className={`${common} text-ink-secondary`} aria-label="Not tried yet" role="img" />;
  }
}
