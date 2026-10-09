// Server-sent events from a POST (ARCHITECTURE.md §10): the slash command stream and the copilot.
// EventSource can only GET, so the body is read by hand: events are separated by a blank line and
// each carries one "data:" line of JSON.

import { ApiError, authorizedFetch } from "@/lib/api";

/** POST `body` to `path`, then hand each event's JSON to onEvent, in order, until the stream ends.
 *  A refusal before the stream starts (403, 404, 422, 503) is thrown as an ApiError. */
export async function postEvents<T>(
  path: string,
  body: unknown,
  onEvent: (event: T) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await authorizedFetch(path, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { Accept: "text/event-stream" },
    signal,
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => null);
    throw new ApiError(
      res.status,
      typeof detail?.detail === "string" ? detail.detail : `Request failed (${res.status})`,
    );
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data: "))
        .map((line) => line.slice(6))
        .join("\n");
      if (!data) continue;
      try {
        onEvent(JSON.parse(data) as T);
      } catch {
        // A frame we can't parse is skipped; the next one still arrives.
      }
    }
  }
}
