import { ArrowUp, Check } from "lucide-react";

import { BrandMark, COMPANY_NAME } from "@/components/brand";
import { cn } from "@/lib/utils";

import styles from "./laptop-hero.module.css";

// The demo story's first customer (§8.2, §11.3): Riya's Aurora 14.
const SERIAL = "AX14-7F3K92";
const TICKET = "SR-2026-00042";

const bubble = "max-w-[85%] rounded-card px-3 py-1.5";
const customerBubble = cn(bubble, "self-end bg-accent text-on-accent");
const supportBubble = cn(bubble, "bg-canvas text-ink dark:bg-surface-raised");

/**
 * A support conversation inside a laptop from sm up; on a phone the conversation card stands on its
 * own at its natural height. Decorative: one label describes it.
 */
export function LaptopHero({ className }: { className?: string }) {
  return (
    <div
      role="img"
      aria-label={`A support chat on a laptop: a customer says their Aurora 14 won't charge, gives the serial number when asked, and gets ticket ${TICKET}.`}
      className={cn("w-full select-none", className)}
    >
      {/* Lid (sm up) */}
      <div className="sm:mx-[5%] sm:rounded-t-card sm:bg-ink sm:p-[2.5%] sm:pt-[2%] sm:dark:bg-surface-raised sm:dark:ring-1 sm:dark:ring-hairline">
        <div className="mx-auto mb-[1.5%] hidden size-1.5 rounded-full bg-ink-secondary/50 sm:block" />

        {/* Screen: exactly 16:10 in the frame, the chat anchored at the bottom as in a real one (were it
            ever taller, its oldest message would leave at the top). On a phone, a card of natural height. */}
        <div className="flex flex-col rounded-card bg-surface text-footnote sm:aspect-[16/10] sm:overflow-hidden sm:rounded-control">
          <div className="flex items-center gap-2 border-b border-hairline px-3 py-2">
            <BrandMark className="size-5" />
            <span className="font-semibold text-ink">{COMPANY_NAME} support</span>
            <span className="ml-auto inline-flex items-center gap-1.5 text-footnote text-ink-secondary">
              <span className="size-1.5 rounded-full bg-success" />
              Online
            </span>
          </div>

          <div className="flex min-h-0 flex-1 flex-col justify-end gap-2 p-3">
            <p className={cn(customerBubble, styles.m1)}>My Aurora 14 won&apos;t charge past 0%.</p>

            <div className="grid justify-items-start">
              <Typing className={styles.t1} />
              <p className={cn(supportBubble, "col-start-1 row-start-1", styles.m2)}>
                Sorry about that. What&apos;s its serial number?
              </p>
            </div>

            <p className={cn(customerBubble, "tabular-nums", styles.m3)}>{SERIAL}</p>

            <div className="grid justify-items-start">
              <Typing className={styles.t2} />
              <div className={cn(supportBubble, "col-start-1 row-start-1", styles.m4)}>
                <p>
                  Thanks, that&apos;s your Aurora 14 (<span className="whitespace-nowrap">{SERIAL}</span>). An
                  agent will confirm a battery replacement here.
                </p>
                <p className="mt-1.5 inline-flex items-center gap-1 rounded-full bg-success/15 px-2 py-0.5 text-footnote font-medium tabular-nums">
                  <Check aria-hidden="true" className="size-3.5 text-success" strokeWidth={3} />
                  Ticket {TICKET}
                </p>
              </div>
            </div>
          </div>

          <div className="mx-3 mb-3 flex items-center rounded-full border border-hairline py-1 pr-1 pl-3 text-ink-secondary">
            Message
            <span className="ml-auto inline-flex size-6 items-center justify-center rounded-full bg-accent text-on-accent">
              <ArrowUp aria-hidden="true" className="size-3.5" strokeWidth={2.5} />
            </span>
          </div>
        </div>
      </div>

      {/* Base (sm up) */}
      <div className="relative hidden h-4 rounded-b-card bg-hairline sm:block">
        <div className="absolute top-0 left-1/2 h-1.5 w-[16%] -translate-x-1/2 rounded-b-control bg-ink-secondary/30" />
      </div>
    </div>
  );
}

function Typing({ className }: { className?: string }) {
  return (
    <span
      className={cn(
        "col-start-1 row-start-1 inline-flex gap-1 rounded-card bg-canvas px-3 py-2.5 dark:bg-surface-raised",
        styles.typing,
        className,
      )}
    >
      <span className={cn("size-1.5 rounded-full bg-ink-secondary", styles.dot)} />
      <span className={cn("size-1.5 rounded-full bg-ink-secondary", styles.dot)} />
      <span className={cn("size-1.5 rounded-full bg-ink-secondary", styles.dot)} />
    </span>
  );
}
