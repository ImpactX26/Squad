// Natural-language ticket search (ARCHITECTURE.md §7.4): POST /api/search, shared by the top bar's
// ⌘K palette and the inbox. A search runs when the agent presses Enter, never per keystroke: each
// one costs a model call (none for a ticket number like SR-2026-00042).

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { components } from "@/lib/api-types";

export type SearchResponse = components["schemas"]["SearchResponse"];
export type SearchResult = components["schemas"]["SearchResult"];

export type SearchState =
  | { state: "idle" }
  | { state: "loading"; query: string }
  | { state: "error"; query: string; message: string }
  | { state: "done"; query: string; response: SearchResponse };

export const SEARCH_EXAMPLES = [
  "open battery tickets from telegram",
  "laptops that overheat and shut down",
  "urgent tickets from email this week",
  "tickets the customer chased",
];

/** The search the agent last submitted, and its result. `initial` runs at once (the inbox's ?q=). */
export function useTicketSearch(initial: string | null = null) {
  const [query, setQuery] = useState<string | null>(initial?.trim() || null);
  const [result, setResult] = useState<SearchState>(
    initial?.trim() ? { state: "loading", query: initial.trim() } : { state: "idle" },
  );
  // Bumped by submit, so searching the same words again (say, after an error) runs again.
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!query) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await api.search(query);
        if (!cancelled) setResult({ state: "done", query, response });
      } catch (err: unknown) {
        if (!cancelled) {
          setResult({ state: "error", query, message: err instanceof Error ? err.message : "The search failed." });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [query, revision]);

  function submit(words: string) {
    const trimmed = words.trim();
    if (!trimmed) return;
    setResult({ state: "loading", query: trimmed });
    setQuery(trimmed);
    setRevision((r) => r + 1);
  }

  function clear() {
    setQuery(null);
    setResult({ state: "idle" });
  }

  return { result, submit, clear };
}
