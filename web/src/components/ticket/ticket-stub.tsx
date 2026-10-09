// The ticket stub (ARCHITECTURE.md §11.3): a gradient top in the ticket's priority colour, a perforated
// tear line, and a stub below. The sign-in page shows a sample; every inbox card is one.

import type { CSSProperties, ReactNode } from "react";

import type { TicketPriority } from "@/lib/api";

export const PRIORITY_GRADIENT: Record<TicketPriority, [string, string]> = {
  urgent: ["var(--prio-urgent-1)", "var(--prio-urgent-2)"],
  high: ["var(--prio-high-1)", "var(--prio-high-2)"],
  medium: ["var(--prio-medium-1)", "var(--prio-medium-2)"],
  low: ["var(--prio-low-1)", "var(--prio-low-2)"],
};

/** The priority colours as CSS variables (--g1, --g2) for a stub and anything inside it. */
export function stubColours(priority: TicketPriority): CSSProperties {
  const [from, to] = PRIORITY_GRADIENT[priority];
  return { "--g1": from, "--g2": to } as CSSProperties;
}

/** The gradient top of a stub. Its words are white and bold, as on a printed ticket. */
export function StubTop({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`relative bg-[linear-gradient(160deg,var(--g1),var(--g2))] text-white ${className}`}>
      {/* A soft light from the top corner, so the colour reads as card stock, not a flat fill. */}
      <span aria-hidden className="pointer-events-none absolute inset-0 bg-[radial-gradient(120%_120%_at_0%_0%,rgb(255_255_255/0.22),transparent_60%)]" />
      <div className="relative">{children}</div>
    </div>
  );
}

/**
 * The tear line: a dashed rule with a half-circle notch cut into each edge. The notches take `notch`,
 * the colour behind the stub, so they look punched through.
 */
export function Perforation({ notch = "var(--canvas)", tone = "rgb(255 255 255 / 0.55)" }: { notch?: string; tone?: string }) {
  return (
    <div aria-hidden className="relative h-0">
      <span className="absolute -top-2 -left-2 size-4 rounded-full" style={{ background: notch }} />
      <span className="absolute -top-2 -right-2 size-4 rounded-full" style={{ background: notch }} />
      <span className="absolute inset-x-4 top-0 border-t-[1.5px] border-dashed" style={{ borderColor: tone }} />
    </div>
  );
}

/** The priority as a small pill on the gradient: white, so it reads on every priority's colour. */
export function StubPriority({ label }: { label: string }) {
  return (
    <span className="inline-flex items-center rounded-full bg-white/24 px-2 py-0.5 text-[11px] font-bold tracking-[0.06em] text-white uppercase ring-1 ring-white/35">
      {label}
    </span>
  );
}
