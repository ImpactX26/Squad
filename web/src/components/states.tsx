/**
 * The one pattern for loading, empty and error states on every staff page (§11.1).
 * Quiet: an icon, a sentence-case title, one line of help, and at most one action.
 */

import type { LucideIcon } from "lucide-react";
import { AlertTriangle, Inbox, LoaderCircle, RotateCw } from "lucide-react";
import type { ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

function StateFrame({
  icon: Icon,
  tone = "neutral",
  title,
  children,
  action,
  className,
  role,
}: {
  icon: LucideIcon;
  tone?: "neutral" | "danger";
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
  role?: "status" | "alert";
}) {
  return (
    <div role={role} className={cn("flex flex-col items-center justify-center px-6 py-12 text-center", className)}>
      <span
        className={cn(
          "inline-flex size-12 items-center justify-center rounded-full",
          tone === "danger" ? "bg-danger/10 text-danger" : "bg-hairline/50 text-ink-secondary",
        )}
      >
        <Icon aria-hidden="true" className="size-6" />
      </span>
      <p className="mt-4 font-semibold text-ink">{title}</p>
      {children && <div className="mt-1 max-w-sm text-subheadline text-pretty text-ink-secondary">{children}</div>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function LoadingState({ label = "Loading", className }: { label?: string; className?: string }) {
  return (
    <div role="status" className={cn("flex items-center justify-center gap-2 px-6 py-12 text-ink-secondary", className)}>
      <LoaderCircle aria-hidden="true" className="size-5 animate-spin" />
      <span className="text-subheadline">{label}</span>
    </div>
  );
}

/** Placeholder rows while a list loads: the list's shape, no spinner jumping around. */
export function LoadingRows({ rows = 5, className }: { rows?: number; className?: string }) {
  return (
    <div role="status" aria-label="Loading" className={cn("divide-y divide-hairline", className)}>
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex gap-3 px-4 py-4">
          <span className="size-9 shrink-0 animate-pulse rounded-control bg-hairline/60" />
          <span className="flex-1 space-y-2">
            <span className="block h-3.5 w-3/5 animate-pulse rounded-full bg-hairline/60" />
            <span className="block h-3 w-2/5 animate-pulse rounded-full bg-hairline/40" />
            <span className="block h-3 w-1/3 animate-pulse rounded-full bg-hairline/40" />
          </span>
        </div>
      ))}
    </div>
  );
}

export function EmptyState({
  icon = Inbox,
  title,
  children,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  children?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <StateFrame icon={icon} title={title} action={action} className={className}>
      {children}
    </StateFrame>
  );
}

export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
  className,
}: {
  title?: string;
  message?: string;
  onRetry?: () => void;
  className?: string;
}) {
  return (
    <StateFrame
      icon={AlertTriangle}
      tone="danger"
      title={title}
      role="alert"
      className={className}
      action={
        onRetry && (
          <Button variant="secondary" size="sm" onClick={onRetry}>
            <RotateCw aria-hidden="true" />
            Try again
          </Button>
        )
      }
    >
      {message}
    </StateFrame>
  );
}
