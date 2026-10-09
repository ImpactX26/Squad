// Slash commands from the composer (ARCHITECTURE.md §7.5): the list from GET /api/commands (the
// built-ins, then the agent's own custom commands), and the SSE stream of
// POST /api/tickets/{id}/commands. The backend decides everything; this only shows its progress.

import type { components } from "@/lib/api-types";
import { postEvents } from "@/lib/sse";

export type Command = components["schemas"]["CommandOut"];
export type CommandList = components["schemas"]["CommandListResponse"];
export type CommandTool = components["schemas"]["CommandToolOut"];

/** Whether the words typed after a command are enough to run it. */
export function hasNeededWords(command: Command, args: string): boolean {
  return command.args !== "required" || args.trim().length > 0;
}

export type CommandStep = { type: "step"; step: string; status: string; detail: string };
export type CommandDone = { type: "done"; outcome: string; message: string; [key: string]: unknown };
export type CommandFailed = { type: "error"; message: string };
export type CommandEvent = CommandStep | CommandDone | CommandFailed;

/** "/payments display replacement" -> { name: "payments", args: "display replacement" }. */
export function parseCommand(text: string): { name: string; args: string } | null {
  const match = /^\/(\S*)(?:\s+([\s\S]*))?$/.exec(text.trim());
  return match ? { name: match[1].toLowerCase(), args: (match[2] ?? "").trim() } : null;
}

/**
 * Run a command and hand each streamed event to onEvent, in order. Resolves when the stream ends.
 * A refusal before the stream starts (403, 404, 503) is thrown as an ApiError.
 */
export async function runCommand(
  ticketId: string,
  name: string,
  args: string,
  onEvent: (event: CommandEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  await postEvents<CommandEvent>(`/api/tickets/${ticketId}/commands`, { name, args }, onEvent, signal);
}
