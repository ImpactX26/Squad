import type { SocketStatus } from "@/lib/ws";

const BADGE: Record<SocketStatus, { dot: string; label: string }> = {
  live: { dot: "bg-success", label: "Live" },
  connecting: { dot: "bg-warning", label: "Connecting" },
  offline: { dot: "bg-danger", label: "Offline, retrying" },
};

/** The /ws/staff connection state. Colour is backed by a word, so it never relies on colour alone. */
export function LiveBadge({ status, className = "" }: { status: SocketStatus; className?: string }) {
  const { dot, label } = BADGE[status];
  return (
    <span role="status" className={`inline-flex items-center gap-1.5 text-footnote text-ink-secondary ${className}`}>
      <span className={`size-1.5 rounded-full ${dot}`} aria-hidden />
      {label}
    </span>
  );
}
