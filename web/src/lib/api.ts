/**
 * The typed fetch wrapper (§11.1). Types come from api-types.ts (`make types`).
 *
 * Staff calls send the signed-in token as a Bearer header; a 401 signs the browser out,
 * which sends the staff pages back to /login.
 */

import { clearSession, getSession } from "@/lib/auth";
import type { components } from "@/lib/api-types";

export type Schemas = components["schemas"];

// NEXT_PUBLIC_* is baked in at build time (§13.3, §18.2). Laptops default to the local API.
export const API_URL = (process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000").replace(/\/$/, "");
export const WS_URL = (process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

type Options = {
  method?: "GET" | "POST" | "PATCH" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
  /** Send the staff token (default). Public calls such as the chat session pass false. */
  auth?: boolean;
};

/** FastAPI's `detail` is a string, or a list of validation errors. */
function messageOf(body: unknown, status: number): string {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail) && typeof detail[0]?.msg === "string") return detail[0].msg;
  if (status >= 500) return "The service is unavailable. Try again shortly.";
  return `Request failed (${status}).`;
}

export async function api<T>(path: string, { method = "GET", body, signal, auth = true }: Options = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (auth) {
    const session = getSession();
    if (session) headers.Authorization = `Bearer ${session.access_token}`;
  }

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (error) {
    if ((error as Error).name === "AbortError") throw error;
    throw new ApiError(0, "Can't reach the server. Check your connection and try again.");
  }

  const parsed: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    if (response.status === 401 && auth) clearSession();
    throw new ApiError(response.status, messageOf(parsed, response.status));
  }
  return parsed as T;
}
