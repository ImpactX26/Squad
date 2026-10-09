// The company's mark and name (§16.2: the working identity is NEXT_PUBLIC_COMPANY_NAME). The mark is four
// dots on a white tile, two violet and two coral, one for each place a customer can reach us; it is also the
// favicon (app/icon.svg).

import Link from "next/link";

import { env } from "@/lib/env";

export function LogoMark({ className = "size-9" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} role="img" aria-label={`${env.companyName} logo`}>
      <rect width="32" height="32" rx="9" fill="#fff" />
      <rect x="0.5" y="0.5" width="31" height="31" rx="8.5" fill="none" stroke="#2c1b64" strokeOpacity=".08" />
      <rect x="8" y="8" width="7" height="7" rx="2.6" fill="#5b4bdb" />
      <rect x="17" y="8" width="7" height="7" rx="2.6" fill="#8f7cf2" />
      <rect x="8" y="17" width="7" height="7" rx="2.6" fill="#f0507a" />
      <rect x="17" y="17" width="7" height="7" rx="2.6" fill="#f2646b" />
    </svg>
  );
}

/** Mark + company name, linking home. */
export function Brand({ size = "md" }: { size?: "md" | "lg" }) {
  return (
    <Link href="/" className="inline-flex items-center gap-2.5 rounded-control outline-none focus-visible:ring-2 focus-visible:ring-accent">
      <LogoMark className={size === "lg" ? "size-11" : "size-8"} />
      <span className={`font-bold tracking-[-0.02em] text-ink ${size === "lg" ? "text-title-2" : "text-body"}`}>
        {env.companyName}
      </span>
    </Link>
  );
}
