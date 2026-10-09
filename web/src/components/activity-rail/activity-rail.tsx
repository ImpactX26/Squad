"use client";

// The Agent Activity rail (ARCHITECTURE.md §11.3): the one place motion lives. Each MCP tool call an
// automation makes on this ticket arrives over /ws/staff as agent.tool_called (§9) and appears in
// order with a short slide and a check. Under prefers-reduced-motion it simply appears.

import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Check, X } from "lucide-react";

export type ToolCall = {
  /** Unique per event, for the list key. */
  key: string;
  tool: string;
  ok: boolean;
  ms: number;
  trigger: string;
  role: string;
  at: string;
};

// The runs that call tools on a ticket (ai_runs.trigger), as the agent would say them.
const TRIGGER_LABEL: Record<string, string> = {
  "payment.paid": "Payment received",
  "/payments": "/payments",
  warranty_repair: "Warranty booking",
  "job.status_changed": "Job update",
  "job.completed": "Job completed",
  "message.received": "Intake",
};

/** "inventory__reserve_part" -> { action: "Reserve part", server: "inventory" }. */
export function describeTool(tool: string): { action: string; server: string } {
  const [server, name = tool] = tool.split("__");
  const words = name.replace(/_/g, " ");
  return { action: words.charAt(0).toUpperCase() + words.slice(1), server };
}

export function ActivityRail({ calls }: { calls: ToolCall[] }) {
  const reduce = useReducedMotion();
  if (!calls.length) {
    return (
      <p className="text-footnote text-ink-secondary">
        Nothing yet. When an automation works on this ticket, each step appears here as it happens.
      </p>
    );
  }
  return (
    <ol className="space-y-1" aria-live="polite" aria-label="Tool calls, oldest first">
      <AnimatePresence initial={false}>
        {calls.map((call, index) => {
          const { action, server } = describeTool(call.tool);
          const newRun = index === 0 || calls[index - 1].trigger !== call.trigger;
          return (
            <motion.li
              key={call.key}
              layout={!reduce}
              initial={reduce ? false : { opacity: 0, x: 12 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }}
              className="list-none"
            >
              {newRun ? (
                <p className="pt-1.5 pb-0.5 text-footnote font-medium text-ink-secondary">
                  {TRIGGER_LABEL[call.trigger] ?? call.trigger}
                </p>
              ) : null}
              <div className="flex items-center gap-2 text-subheadline">
                <motion.span
                  initial={reduce ? false : { scale: 0.4, opacity: 0 }}
                  animate={{ scale: 1, opacity: 1 }}
                  transition={{ delay: reduce ? 0 : 0.12, duration: 0.18 }}
                  className={`grid size-4 shrink-0 place-items-center rounded-full ${call.ok ? "bg-success" : "bg-danger"}`}
                >
                  {call.ok ? (
                    <Check className="size-3 text-white" strokeWidth={3} aria-label="Succeeded" />
                  ) : (
                    <X className="size-3 text-white" strokeWidth={3} aria-label="Failed" />
                  )}
                </motion.span>
                <span className="min-w-0 flex-1 truncate text-ink">{action}</span>
                <span className="shrink-0 text-footnote tabular-nums text-ink-secondary">
                  {server} · {call.ms} ms
                </span>
              </div>
            </motion.li>
          );
        })}
      </AnimatePresence>
    </ol>
  );
}
