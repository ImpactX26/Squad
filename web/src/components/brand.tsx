import { cn } from "@/lib/utils";

// NEXT_PUBLIC_* is baked in at build time (§13.3, §18.2).
export const COMPANY_NAME = process.env.NEXT_PUBLIC_COMPANY_NAME || "Aurora Devices";

/** The company mark. Same drawing as src/app/icon.svg (the favicon and the PDF receipt's logo). */
export function BrandMark({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" aria-hidden="true" className={cn("size-7 shrink-0", className)}>
      <rect width="32" height="32" rx="8" className="fill-accent" />
      <path
        d="M9.5 23.5 16 9l6.5 14.5"
        fill="none"
        strokeWidth="3"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="stroke-on-accent"
      />
      <path
        d="M11.7 18.6Q16 15.6 20.3 18.6"
        fill="none"
        strokeWidth="2.4"
        strokeLinecap="round"
        className="stroke-on-accent"
      />
    </svg>
  );
}

/** The mark and the company name, as shown in page headers. */
export function Brand({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <BrandMark />
      <span className="text-body font-semibold tracking-tight text-ink">{COMPANY_NAME}</span>
    </span>
  );
}
