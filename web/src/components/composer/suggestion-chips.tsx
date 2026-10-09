"use client";

// Suggested action chips above the composer (ARCHITECTURE.md §7.5, §11.3): GET
// /api/tickets/{id}/suggestions, fetched again when the ticket changes. Clicking one runs that
// command, exactly as typing it would; /escalate and /close fill the composer for the agent's words.

import { Sparkles } from "lucide-react";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { components } from "@/lib/api-types";

export type Chip = components["schemas"]["SuggestionChip"];

type Load =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; chips: Chip[]; source: "ai" | "rules" };

export function SuggestionChips({
  ticketId,
  refreshKey,
  disabled,
  onPick,
}: {
  ticketId: string;
  refreshKey: string;
  disabled: boolean;
  onPick: (chip: Chip) => void;
}) {
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const body = await api.suggestions(ticketId);
        if (!cancelled) setLoad({ state: "ready", chips: body.chips, source: body.source });
      } catch (err: unknown) {
        // A failed refresh keeps the chips already shown.
        if (!cancelled) {
          setLoad((current) =>
            current.state === "ready"
              ? current
              : { state: "error", message: err instanceof Error ? err.message : "Couldn’t load suggestions." },
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [ticketId, refreshKey, retry]);

  if (load.state === "loading") {
    return (
      <div className="flex gap-2" aria-busy="true" aria-label="Loading suggestions">
        {[0, 1, 2].map((i) => (
          <span key={i} className="h-7 w-28 rounded-full bg-canvas dark:bg-surface-raised" />
        ))}
      </div>
    );
  }
  if (load.state === "error") {
    return (
      <p role="alert" className="text-footnote text-ink-secondary">
        Couldn’t load suggestions: {load.message}{" "}
        <button type="button" onClick={() => setRetry((n) => n + 1)} className="text-accent outline-none hover:underline focus-visible:underline">
          Try again
        </button>
      </p>
    );
  }
  if (!load.chips.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="Suggested actions">
      <Sparkles
        className="size-3.5 text-ink-secondary"
        aria-label={load.source === "ai" ? "Suggested by AI" : "Suggested (AI unavailable, default order)"}
      />
      {load.chips.map((chip) => (
        <button
          key={`${chip.name}:${chip.args}`}
          type="button"
          disabled={disabled}
          onClick={() => onPick(chip)}
          title={chip.needs_args ? `Type the words after /${chip.name}` : `Runs /${chip.name}${chip.args ? ` ${chip.args}` : ""}`}
          className="rounded-full bg-accent/10 px-3 py-1 text-footnote font-medium text-accent outline-none transition-colors hover:bg-accent/16 focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50"
        >
          {chip.label}
        </button>
      ))}
    </div>
  );
}
