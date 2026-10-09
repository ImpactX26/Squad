// The one loading, error and empty pattern every staff page uses (section 14.2, Phase 4).
//
//   loading -> LoadingState: quiet surface blocks in the shape of what is coming, aria-busy
//   failed  -> ErrorState:   what went wrong, in the server's words, and "Try again"
//   nothing -> EmptyState:   what would be here, and how it gets here
//
// A page that already shows data and fails to refresh keeps the data and shows InlineError above it.

import { AlertTriangle, type LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";

export function LoadingState({
  label,
  rows = 3,
  rowClassName = "h-16",
}: {
  /** Read by screen readers, e.g. "Loading jobs". */
  label: string;
  rows?: number;
  rowClassName?: string;
}) {
  return (
    <div className="space-y-3" aria-busy="true" aria-label={label}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className={`rounded-card bg-surface ${rowClassName}`} />
      ))}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  action,
}: {
  message: string;
  onRetry?: () => void;
  /** Replaces "Try again" when retrying can't help, e.g. a link back. */
  action?: ReactNode;
}) {
  return (
    <div role="alert" className="grid place-items-center gap-3 rounded-panel bg-surface px-4 py-12 text-center">
      <AlertTriangle className="size-6 text-danger" aria-hidden />
      <p className="max-w-md text-subheadline text-ink-secondary">{message}</p>
      {action ??
        (onRetry ? (
          <Button variant="outline" onClick={onRetry} className="rounded-control">
            Try again
          </Button>
        ) : null)}
    </div>
  );
}

export function EmptyState({
  icon: Icon,
  title,
  hint,
  action,
}: {
  icon: LucideIcon;
  title: string;
  hint?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="grid place-items-center gap-2 rounded-panel bg-surface px-4 py-12 text-center">
      <Icon className="size-6 text-ink-secondary" aria-hidden />
      <p className="text-subheadline text-ink">{title}</p>
      {hint ? <p className="max-w-md text-footnote text-ink-secondary">{hint}</p> : null}
      {action ? <div className="pt-2">{action}</div> : null}
    </div>
  );
}

/** A failed refresh while the last good data stays on screen. */
export function InlineError({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="flex items-center gap-3 rounded-card bg-surface px-4 py-3 text-subheadline">
      <AlertTriangle className="size-4 shrink-0 text-danger" aria-hidden />
      <span className="min-w-0 flex-1 text-ink-secondary">{message}</span>
      {onRetry ? (
        <Button variant="outline" size="sm" onClick={onRetry} className="shrink-0">
          Try again
        </Button>
      ) : null}
    </div>
  );
}

/** The message to show for anything a fetch threw. */
export function errorMessage(err: unknown, fallback: string): string {
  return err instanceof Error && err.message ? err.message : fallback;
}
