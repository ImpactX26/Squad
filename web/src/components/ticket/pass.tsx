// The ticket pass (ARCHITECTURE.md §11.3): the whole card in one colour, like a printed ticket. A top with
// the ServiceMesh line and a date, the big reference number; a perforated tear line with notches; a
// lighter lower half with small labelled fields and the person it is about. Inbox cards, technician
// jobs, and the ticket and job page headers are built from these parts.

import { Hash } from "lucide-react";
import type { CSSProperties, ReactNode } from "react";

import type { TicketPriority } from "@/lib/api";

export type PassTone = "red" | "orange" | "yellow" | "green" | "violet" | "slate";

/** Priority, as the pass's colour: urgent red, high orange, medium yellow, low green. */
export const PRIORITY_TONE: Record<TicketPriority, PassTone> = {
  urgent: "red",
  high: "orange",
  medium: "yellow",
  low: "green",
};

// Yellow is the one colour white words don't read on; its top uses dark ones.
const DARK_TOP: ReadonlySet<PassTone> = new Set(["yellow"]);

export function passStyle(tone: PassTone): CSSProperties {
  return { "--p1": `var(--pass-${tone}-1)`, "--p2": `var(--pass-${tone}-2)` } as CSSProperties;
}

/** The card itself. `as` lets an inbox card be a link and a page header a plain section. */
export function Pass({
  tone,
  children,
  className = "",
  style,
}: {
  tone: PassTone;
  children: ReactNode;
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <div
      style={{ ...passStyle(tone), ...style }}
      data-tone={tone}
      className={`relative flex flex-col overflow-hidden rounded-[26px] bg-[linear-gradient(165deg,var(--p1),var(--p2))] ring-1 ring-[color-mix(in_oklab,var(--p1)_70%,black_8%)] ${className}`}
    >
      {children}
    </div>
  );
}

/** The coloured top. Its words are white, or dark on yellow. */
export function PassTop({ tone, children, className = "" }: { tone: PassTone; children: ReactNode; className?: string }) {
  return (
    <div className={`relative ${DARK_TOP.has(tone) ? "text-[#3b2f05]" : "text-white"} ${className}`}>
      {/* A soft light from the top corner, so the colour reads as card stock rather than a flat fill. */}
      <span aria-hidden className="pointer-events-none absolute inset-0 bg-[radial-gradient(130%_120%_at_0%_0%,rgb(255_255_255/0.2),transparent_60%)]" />
      <div className="relative">{children}</div>
    </div>
  );
}

/** "# SERVICEMESH ……… OCT 09": the printed line along the top of every pass. */
export function PassBrand({ right }: { right?: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-[12px] font-bold tracking-[0.08em] uppercase opacity-90">
      <span className="flex items-center gap-1.5">
        <Hash className="size-3.5" strokeWidth={2.75} aria-hidden />
        ServiceMesh
      </span>
      {right ? <span className="tabular-nums">{right}</span> : null}
    </div>
  );
}

/** The priority or status as an outlined pill on the coloured top. */
export function PassPill({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-full bg-white/22 px-2.5 py-0.5 text-[12px] font-bold tracking-[0.06em] uppercase ring-[1.5px] ring-current/45">
      {children}
    </span>
  );
}

/** The tear line: dashes between two punched notches, which take the colour behind the card. */
export function PassTear({ notch = "var(--canvas)" }: { notch?: string }) {
  return (
    <div aria-hidden className="relative h-0">
      <span className="absolute -top-3 -left-3 size-6 rounded-full" style={{ background: notch }} />
      <span className="absolute -top-3 -right-3 size-6 rounded-full" style={{ background: notch }} />
      <span className="absolute inset-x-6 top-0 border-t-2 border-dashed border-white/55" />
    </div>
  );
}

/** The lighter lower half, with ink words so the details read on every colour. */
export function PassBottom({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`relative flex flex-1 flex-col bg-white/62 text-[#1e1a33] dark:bg-[color-mix(in_oklab,var(--surface)_80%,transparent)] dark:text-ink ${className}`}>
      {children}
    </div>
  );
}

/** Small labelled fields in a row (Channel, Type, Status), as on a boarding pass. */
export function PassFields({ fields }: { fields: { label: string; value: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-3 gap-x-2.5 gap-y-1">
      {fields.map(({ label, value }) => (
        <div key={label} className="min-w-0">
          <dt className="text-[10.5px] font-bold tracking-[0.08em] uppercase opacity-60">{label}</dt>
          {/* Wraps to a second line rather than cutting a value off ("Awaiting customer", "Vertex Gaming 17"). */}
          <dd className="line-clamp-2 text-[13px] leading-snug font-semibold">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** "MN Meera Nair ……… 4 min ago": who the pass is about, with their initials. */
export function PassPerson({ name, trailing }: { name: string | null | undefined; trailing?: ReactNode }) {
  const shown = name?.trim() || "Customer";
  return (
    <div className="flex items-center gap-2.5 border-t border-[#1e1a33]/10 pt-3 dark:border-white/10">
      <span className="grid size-7 shrink-0 place-items-center rounded-full bg-white text-[11px] font-bold tracking-[0.02em] text-[#1e1a33] shadow-[0_1px_3px_rgb(0_0_0/0.12)]">
        {initials(shown)}
      </span>
      <span className="min-w-0 flex-1 truncate text-[14px] font-semibold">{shown}</span>
      {trailing ? <span className="shrink-0 text-[13px] tabular-nums opacity-65">{trailing}</span> : null}
    </div>
  );
}

export function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("") || "?";
}

/** "OCT 09", the printed date on a pass. */
export function passDate(iso: string): string {
  const day = new Date(iso.length === 10 ? `${iso}T00:00:00` : iso);
  return `${day.toLocaleDateString("en", { month: "short" })} ${String(day.getDate()).padStart(2, "0")}`;
}
