"use client";

// The top bar's ⌘K search (ARCHITECTURE.md §7.4, §11.3): plain words in, ticket cards out, each
// with its "why this matched". Enter on new words searches (one model call); with results showing,
// the arrow keys and Enter open a ticket. A Radix dialog with a cmdk list inside, so it has a
// proper title for screen readers.

import { Command } from "cmdk";
import { CornerDownLeft, LoaderCircle, Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { Dialog } from "radix-ui";
import { useEffect, useState } from "react";

import { StatusPill } from "@/components/ticket/glyphs";
import { SEARCH_EXAMPLES, useTicketSearch } from "@/lib/search";

const ITEM =
  "flex cursor-pointer flex-col gap-0.5 rounded-[8px] px-3 py-2 text-subheadline text-ink outline-none data-[selected=true]:bg-accent/12";

export function SearchPalette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [words, setWords] = useState("");
  const [selected, setSelected] = useState("");
  const { result, submit } = useTicketSearch();

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setOpen((current) => !current);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const searched = result.state === "idle" ? null : result.query;
  // New words: Enter searches. The words just searched: Enter opens the highlighted result.
  const fresh = words.trim().length > 0 && words.trim() !== searched;

  function go(path: string) {
    setOpen(false);
    router.push(path);
  }

  function run(text: string) {
    setWords(text);
    setSelected("");
    submit(text);
  }

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger
        className="hidden items-center gap-2 rounded-control bg-canvas py-1 pr-1.5 pl-2.5 text-footnote text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent sm:inline-flex dark:bg-surface-raised"
        aria-label="Search tickets"
      >
        <Search className="size-3.5" aria-hidden />
        Search
        <kbd className="rounded-[5px] bg-surface px-1.5 font-sans text-[11px] dark:bg-surface">⌘K</kbd>
      </Dialog.Trigger>
      <Dialog.Trigger
        className="grid size-8 place-items-center rounded-full text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent sm:hidden"
        aria-label="Search tickets"
      >
        <Search className="size-4" aria-hidden />
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/25 animate-in fade-in" />
        <Dialog.Content className="fixed top-[10vh] left-1/2 z-50 w-[min(42rem,calc(100vw-2rem))] -translate-x-1/2 overflow-hidden rounded-card bg-surface-raised shadow-raised outline-none animate-in fade-in slide-in-from-top-2">
          <Dialog.Title className="sr-only">Search tickets</Dialog.Title>
          <Dialog.Description className="sr-only">
            Type what you are looking for in plain words and press Enter.
          </Dialog.Description>
          <Command label="Search tickets" shouldFilter={false} value={selected} onValueChange={setSelected}>
            <div className="flex items-center gap-2 border-b border-hairline px-4">
              <Search className="size-4 shrink-0 text-ink-secondary" aria-hidden />
              <Command.Input
                value={words}
                onValueChange={setWords}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && fresh) {
                    event.preventDefault();  // cmdk then leaves it alone
                    run(words);
                  }
                }}
                placeholder="Search in plain words, a ticket number or a serial, e.g. open battery tickets"
                className="h-12 w-full bg-transparent text-body text-ink outline-none placeholder:text-subheadline placeholder:text-ink-secondary"
              />
              {fresh ? (
                <span className="inline-flex shrink-0 items-center gap-1 text-footnote text-ink-secondary">
                  <CornerDownLeft className="size-3.5" aria-hidden /> to search
                </span>
              ) : null}
            </div>

            <Command.List className="max-h-[60vh] overflow-y-auto p-1">
              {result.state === "idle" ? (
                <Command.Group heading="Try" className="text-footnote text-ink-secondary [&_[cmdk-group-heading]]:px-3 [&_[cmdk-group-heading]]:py-1.5">
                  {SEARCH_EXAMPLES.map((example) => (
                    <Command.Item key={example} value={`example:${example}`} onSelect={() => run(example)} className={ITEM}>
                      {example}
                    </Command.Item>
                  ))}
                </Command.Group>
              ) : null}

              {result.state === "loading" ? (
                <p role="status" className="flex items-center gap-2 px-3 py-3 text-subheadline text-ink-secondary">
                  <LoaderCircle className="size-4 animate-spin" aria-hidden /> Searching…
                </p>
              ) : null}

              {result.state === "error" ? (
                <div role="alert" className="space-y-2 px-3 py-3">
                  <p className="text-subheadline text-ink-secondary">{result.message}</p>
                  <Command.Item value="retry" onSelect={() => run(result.query)} className={ITEM}>
                    Try again
                  </Command.Item>
                </div>
              ) : null}

              {result.state === "done" ? (
                <>
                  <p className="px-3 pt-2 pb-1 text-footnote text-ink-secondary">
                    {result.response.notice ? `${result.response.notice} ` : ""}
                    Searched: {result.response.interpretation.summary}
                  </p>
                  {result.response.results.length === 0 ? (
                    <p className="px-3 py-3 text-subheadline text-ink-secondary">No tickets match.</p>
                  ) : (
                    result.response.results.map(({ ticket, why }) => (
                      <Command.Item
                        key={ticket.id}
                        value={ticket.id}
                        onSelect={() => go(`/tickets/${ticket.id}`)}
                        className={ITEM}
                      >
                        <span className="flex items-baseline gap-2">
                          <span className="shrink-0 text-footnote tabular-nums text-ink-secondary">{ticket.ticket_number}</span>
                          <span className="min-w-0 flex-1 truncate font-medium">{ticket.title}</span>
                          <StatusPill status={ticket.status} />
                        </span>
                        <span className="truncate text-footnote text-ink-secondary">
                          {[ticket.customer?.full_name,
                            ticket.product ? `${ticket.product.model_name} · ${ticket.product.serial_number}` : null]
                            .filter(Boolean).join(" — ")}
                        </span>
                        <span className="text-footnote text-accent">{why}</span>
                      </Command.Item>
                    ))
                  )}
                  <Command.Item
                    value="inbox"
                    onSelect={() => go(`/inbox?q=${encodeURIComponent(result.query)}`)}
                    className={`${ITEM} text-accent`}
                  >
                    Show these results in the inbox
                  </Command.Item>
                </>
              ) : null}
            </Command.List>
          </Command>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
