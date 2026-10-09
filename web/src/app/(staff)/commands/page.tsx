"use client";

// The command editor (ARCHITECTURE.md §7.5, §11.2): the built-ins, and the agent's own custom slash
// commands, which they create, edit, test on a ticket and delete. GET/POST/PATCH/DELETE /api/commands.
//
// The backend enforces the rules (§4.6): a template uses only the listed {{variables}}, plain
// substitution and never eval; allowed_tools are the only tools the model is offered, and the tools
// no model may use (marking payments paid, UTRs, stock and dispatch writers) are never listed here.

import { FlaskConical, LoaderCircle, Pencil, Plus, SquareSlash, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { type CommandRun, CommandRunPanel } from "@/components/composer/slash-commands";
import { EmptyState, ErrorState, InlineError, LoadingState, errorMessage } from "@/components/states";
import { Button } from "@/components/ui/button";
import { type Ticket, api } from "@/lib/api";
import { type Command, type CommandList, type CommandTool, runCommand } from "@/lib/commands";
import { useStaffUser } from "@/lib/session";

type Load = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; list: CommandList };
type Draft = { id: string | null; name: string; description: string; prompt_template: string; allowed_tools: string[] };

const FIELD =
  "w-full rounded-control bg-canvas px-3 py-2 text-subheadline text-ink placeholder:text-ink-secondary " +
  "outline-none focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised";

const EXAMPLE: Draft = {
  id: null,
  name: "warranty-check",
  description: "Check the warranty and tell the customer whether the repair is free",
  prompt_template:
    "Check the warranty for ticket {{ticket.number}} (serial {{product.serial}}).\n" +
    "If it is in warranty, tell the customer the repair is free and ask for a preferred visit date.\n" +
    "If not, tell them the repair cost for {{ticket.issue_type}} and ask whether to proceed.",
  allowed_tools: ["catalog__lookup_serial", "catalog__get_service_price", "messaging__send_reply"],
};

export default function CommandsPage() {
  const role = useStaffUser()?.role;
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [testing, setTesting] = useState<Command | null>(null);
  const [deleting, setDeleting] = useState<string | null>(null);
  // Bumped to refetch; the fetch itself lives in the effect, like the inbox's.
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((r) => r + 1), []);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.commands();
        if (cancelled) return;
        setLoad({ state: "ready", list });
        setRefreshError(null);
      } catch (err: unknown) {
        if (cancelled) return;
        const message = errorMessage(err, "Couldn’t load the commands.");
        setLoad((current) => (current.state === "ready" ? current : { state: "error", message }));
        setRefreshError(message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [revision]);

  async function remove(command: Command) {
    if (!command.id || !window.confirm(`Delete /${command.name}? This can’t be undone.`)) return;
    setDeleting(command.id);
    try {
      await api.deleteCommand(command.id);
      if (testing?.id === command.id) setTesting(null);
      if (draft?.id === command.id) setDraft(null);
      refresh();
    } catch (err: unknown) {
      setRefreshError(errorMessage(err, "Couldn’t delete that command."));
    } finally {
      setDeleting(null);
    }
  }

  if (role && role !== "agent" && role !== "admin") {
    return <ErrorState message="Slash commands are for agents and admins." />;
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="flex flex-wrap items-end gap-3">
        <div className="space-y-1">
          <h1 className="text-title-1 font-semibold tracking-tight text-ink">Commands</h1>
          <p className="text-subheadline text-ink-secondary">
            Type <span className="font-medium text-ink">/</span> in a ticket’s composer to run one.
          </p>
        </div>
        {load.state === "ready" && !draft ? (
          <Button onClick={() => setDraft({ ...EXAMPLE, name: "" })} className="ml-auto rounded-control text-[15px]">
            <Plus aria-hidden /> New command
          </Button>
        ) : null}
      </header>

      {load.state === "loading" ? <LoadingState label="Loading commands" rows={4} rowClassName="h-14" /> : null}
      {load.state === "error" ? <ErrorState message={load.message} onRetry={() => refresh()} /> : null}

      {load.state === "ready" ? (
        <>
          {refreshError ? <InlineError message={refreshError} onRetry={() => refresh()} /> : null}

          {draft ? (
            <Editor
              key={draft.id ?? "new"}
              initial={draft}
              list={load.list}
              onCancel={() => setDraft(null)}
              onSaved={(saved) => {
                setDraft(null);
                setTesting(saved);
                refresh();
              }}
            />
          ) : null}

          {testing ? <TestRun key={testing.name} command={testing} onClose={() => setTesting(null)} /> : null}

          <Section title="Your commands">
            {mine(load.list).length === 0 ? (
              <EmptyState
                icon={SquareSlash}
                title="You have no commands of your own yet."
                hint="A command is a prompt with {{variables}} that the copilot runs on a ticket, with only the tools you allow."
                action={
                  draft ? null : (
                    <Button variant="outline" onClick={() => setDraft(EXAMPLE)} className="rounded-control">
                      Start from an example
                    </Button>
                  )
                }
              />
            ) : (
              <ul className="divide-y divide-hairline overflow-hidden rounded-card bg-surface">
                {mine(load.list).map((command) => (
                  <li key={command.id} className="flex flex-wrap items-start gap-3 px-4 py-3">
                    <div className="min-w-0 flex-1 space-y-0.5">
                      <p className="text-subheadline font-medium text-ink">{command.usage}</p>
                      <p className="text-footnote text-ink-secondary">{command.description}</p>
                      <p className="text-footnote text-ink-secondary">
                        {command.allowed_tools.length
                          ? `Tools: ${command.allowed_tools.join(", ")}`
                          : "No tools: the copilot only answers."}
                      </p>
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <Button variant="ghost" size="sm" onClick={() => setTesting(command)}>
                        <FlaskConical aria-hidden /> Test
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          setDraft({
                            id: command.id ?? null,
                            name: command.name,
                            description: command.description,
                            prompt_template: command.prompt_template ?? "",
                            allowed_tools: command.allowed_tools,
                          })
                        }
                      >
                        <Pencil aria-hidden /> Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => void remove(command)}
                        disabled={deleting === command.id}
                        aria-label={`Delete /${command.name}`}
                      >
                        {deleting === command.id ? <LoaderCircle className="animate-spin" aria-hidden /> : <Trash2 aria-hidden />}
                        Delete
                      </Button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Built in">
            <ul className="divide-y divide-hairline overflow-hidden rounded-card bg-surface">
              {load.list.commands
                .filter((c) => c.is_builtin)
                .map((command) => (
                  <li key={command.name} className="space-y-0.5 px-4 py-3">
                    <p className="text-subheadline font-medium text-ink">{command.usage}</p>
                    <p className="text-footnote text-ink-secondary">{command.description}</p>
                  </li>
                ))}
            </ul>
          </Section>
        </>
      ) : null}
    </div>
  );
}

const mine = (list: CommandList) => list.commands.filter((c) => !c.is_builtin);

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="space-y-2">
      <h2 className="text-title-2 font-semibold tracking-tight text-ink">{title}</h2>
      {children}
    </section>
  );
}

function Editor({
  initial,
  list,
  onCancel,
  onSaved,
}: {
  initial: Draft;
  list: CommandList;
  onCancel: () => void;
  onSaved: (command: Command) => void;
}) {
  const [draft, setDraft] = useState<Draft>(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const template = useRef<HTMLTextAreaElement>(null);

  const byServer = useMemo(() => {
    const groups = new Map<string, CommandTool[]>();
    for (const tool of list.tools) groups.set(tool.server, [...(groups.get(tool.server) ?? []), tool]);
    return [...groups.entries()];
  }, [list.tools]);

  function insert(variable: string) {
    const box = template.current;
    const token = `{{${variable}}}`;
    const at = box?.selectionStart ?? draft.prompt_template.length;
    const end = box?.selectionEnd ?? at;
    const next = draft.prompt_template.slice(0, at) + token + draft.prompt_template.slice(end);
    setDraft({ ...draft, prompt_template: next });
    requestAnimationFrame(() => {
      box?.focus();
      box?.setSelectionRange(at + token.length, at + token.length);
    });
  }

  function toggle(tool: string) {
    setDraft((d) => ({
      ...d,
      allowed_tools: d.allowed_tools.includes(tool) ? d.allowed_tools.filter((t) => t !== tool) : [...d.allowed_tools, tool],
    }));
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    const body = {
      name: draft.name.trim().replace(/^\//, "").toLowerCase(),
      description: draft.description.trim(),
      prompt_template: draft.prompt_template,
      allowed_tools: draft.allowed_tools,
    };
    try {
      onSaved(draft.id ? await api.editCommand(draft.id, body) : await api.createCommand(body));
    } catch (err: unknown) {
      setError(errorMessage(err, "Couldn’t save the command."));
    } finally {
      setSaving(false);
    }
  }

  // A tool the command lists that the servers don't offer right now (a server is down).
  const unknown = draft.allowed_tools.filter((t) => !list.tools.some((tool) => tool.name === t));

  return (
    <form onSubmit={save} aria-label={draft.id ? `Edit /${initial.name}` : "New command"} className="space-y-4 rounded-panel bg-surface p-5">
      <h2 className="text-title-2 font-semibold tracking-tight text-ink">{draft.id ? `Edit /${initial.name}` : "New command"}</h2>

      <div className="grid gap-4 sm:grid-cols-[14rem_minmax(0,1fr)]">
        <label className="space-y-1.5">
          <span className="text-footnote font-medium text-ink-secondary">Name</span>
          <span className="flex items-center rounded-control bg-canvas pl-3 focus-within:ring-2 focus-within:ring-accent dark:bg-surface-raised">
            <span className="text-subheadline text-ink-secondary">/</span>
            <input
              required
              value={draft.name}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              placeholder="warranty-check"
              pattern="[a-z0-9][a-z0-9\-]{0,39}"
              title="Lowercase letters, digits and dashes"
              className="w-full bg-transparent py-2 pr-3 pl-0.5 text-subheadline text-ink outline-none placeholder:text-ink-secondary"
            />
          </span>
        </label>
        <label className="space-y-1.5">
          <span className="text-footnote font-medium text-ink-secondary">Description</span>
          <input
            required
            maxLength={200}
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
            placeholder="What it does, in one line"
            className={FIELD}
          />
        </label>
      </div>

      <div className="space-y-1.5">
        <label htmlFor="command-template" className="text-footnote font-medium text-ink-secondary">
          Prompt template
        </label>
        <textarea
          id="command-template"
          ref={template}
          required
          rows={6}
          maxLength={4000}
          value={draft.prompt_template}
          onChange={(e) => setDraft({ ...draft, prompt_template: e.target.value })}
          className={`${FIELD} resize-y font-mono text-footnote`}
        />
        <div className="flex flex-wrap gap-1.5" aria-label="Insert a variable">
          {Object.entries(list.variables).map(([variable, meaning]) => (
            <button
              key={variable}
              type="button"
              title={meaning}
              onClick={() => insert(variable)}
              className="rounded-full bg-canvas px-2 py-0.5 font-mono text-footnote text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised"
            >
              {`{{${variable}}}`}
            </button>
          ))}
        </div>
      </div>

      <fieldset className="space-y-2">
        <legend className="text-footnote font-medium text-ink-secondary">
          Tools the copilot may use (only these; payment, stock and dispatch writers are never offered)
        </legend>
        {list.unreachable_servers.length ? (
          <p className="text-footnote text-warning">
            Not listed because their MCP servers aren’t reachable right now: {list.unreachable_servers.join(", ")}.
          </p>
        ) : null}
        {byServer.length === 0 ? (
          <p className="text-footnote text-ink-secondary">No tools are available: the MCP servers aren’t running.</p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-2">
            {byServer.map(([server, tools]) => (
              <div key={server} className="space-y-1 rounded-card bg-canvas p-3 dark:bg-surface-raised">
                <p className="text-footnote font-semibold text-ink">{server}</p>
                {tools.map((tool) => (
                  <label key={tool.name} className="flex items-start gap-2 text-footnote">
                    <input
                      type="checkbox"
                      checked={draft.allowed_tools.includes(tool.name)}
                      onChange={() => toggle(tool.name)}
                      className="mt-0.5 accent-accent"
                    />
                    <span>
                      <span className="text-ink">{tool.name.split("__")[1]}</span>
                      <span className="text-ink-secondary"> · {tool.description}</span>
                    </span>
                  </label>
                ))}
              </div>
            ))}
          </div>
        )}
        {unknown.length ? (
          <p className="text-footnote text-ink-secondary">Also listed, not offered right now: {unknown.join(", ")}</p>
        ) : null}
      </fieldset>

      {error ? (
        <p role="alert" className="rounded-control bg-danger/10 px-3 py-2 text-subheadline text-ink">
          {error}
        </p>
      ) : null}

      <div className="flex justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onCancel} className="rounded-control">
          Cancel
        </Button>
        <Button type="submit" disabled={saving} className="rounded-control text-[15px]">
          {saving ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
          Save
        </Button>
      </div>
    </form>
  );
}

function TestRun({ command, onClose }: { command: Command; onClose: () => void }) {
  const [tickets, setTickets] = useState<Ticket[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [ticketId, setTicketId] = useState("");
  const [args, setArgs] = useState("");
  const [run, setRun] = useState<CommandRun | null>(null);

  useEffect(() => {
    api
      .tickets({ open_only: true, limit: 50 })
      .then((body) => {
        setTickets(body.tickets);
        setTicketId(body.tickets[0]?.id ?? "");
      })
      .catch((err: unknown) => setLoadError(errorMessage(err, "Couldn’t load the open tickets.")));
  }, []);

  async function start(event: React.FormEvent) {
    event.preventDefault();
    if (!ticketId || run?.state === "running") return;
    setRun({ command: command.name, args, state: "running", events: [] });
    let failed = false;
    try {
      await runCommand(ticketId, command.name, args, (e) => {
        if (e.type === "error") failed = true;
        setRun((current) => (current ? { ...current, events: [...current.events, e] } : current));
      });
      setRun((current) => (current ? { ...current, state: failed ? "error" : "done" } : current));
    } catch (err: unknown) {
      setRun((current) =>
        current
          ? { ...current, state: "error", events: [...current.events, { type: "error", message: errorMessage(err, "The test failed.") }] }
          : current,
      );
    }
  }

  return (
    <section aria-label={`Test /${command.name}`} className="space-y-3 rounded-panel bg-surface p-5">
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-title-2 font-semibold tracking-tight text-ink">Test /{command.name}</h2>
          <p className="text-footnote text-ink-secondary">
            This really runs on the ticket you pick: anything it sends reaches that customer.
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </div>
      {loadError ? <InlineError message={loadError} /> : null}
      {tickets && tickets.length === 0 ? <p className="text-footnote text-ink-secondary">There are no open tickets to test on.</p> : null}
      <form onSubmit={start} className="flex flex-wrap items-end gap-2">
        <label className="min-w-0 flex-1 space-y-1.5">
          <span className="text-footnote font-medium text-ink-secondary">Ticket</span>
          <select
            value={ticketId}
            onChange={(e) => setTicketId(e.target.value)}
            disabled={!tickets?.length}
            className={FIELD}
          >
            {tickets === null && !loadError ? <option>Loading…</option> : null}
            {tickets?.map((t) => (
              <option key={t.id} value={t.id}>
                {t.ticket_number} · {t.title}
              </option>
            ))}
          </select>
        </label>
        <label className="min-w-0 flex-1 space-y-1.5">
          <span className="text-footnote font-medium text-ink-secondary">Words after the command (optional)</span>
          <input value={args} onChange={(e) => setArgs(e.target.value)} className={FIELD} placeholder="{{args}}" />
        </label>
        <Button type="submit" disabled={!ticketId || run?.state === "running"} className="rounded-control text-[15px]">
          {run?.state === "running" ? <LoaderCircle className="animate-spin" aria-hidden /> : <FlaskConical aria-hidden />}
          Run test
        </Button>
      </form>
      {run ? <CommandRunPanel run={run} onDismiss={() => setRun(null)} /> : null}
    </section>
  );
}
