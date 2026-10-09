// The public pay API (ARCHITECTURE.md sections 7.6 and 10), as the /pay/[token] page calls it.
//
// Paths are relative on purpose: this web server proxies /api/pay/* to the backend (next.config.ts),
// so a phone that reaches the page through a tunnel reaches its API through the same origin. No
// staff token is sent; the link's token is the only key.

import type { components } from "@/lib/api-types";

export type PayInvoice = components["schemas"]["PayInvoice"];
export type PayStatus = components["schemas"]["PayStatus"];
export type UtrResult = components["schemas"]["UtrOut"];
export type PaymentStatus = PayInvoice["status"];

/** A refused or failed call, with what the server said and what the page needs to react to it. */
export class PayError extends Error {
  constructor(
    public status: number,
    message: string,
    /** Seconds from Retry-After on a 429. */
    public retryAfter: number | null = null,
    /** submit_utr's refusal code: expired, already_paid, cancelled, utr_used, too_many_attempts, ... */
    public code: string | null = null,
    public attemptsLeft: number | null = null,
  ) {
    super(message);
  }
}

// Only for responses that carry no message of their own (the proxy's bare 500, a validation list).
const FALLBACK: Record<number, string> = {
  404: "This payment link is not valid.",
  422: "Enter the 12-digit UTR (UPI reference number) from your payment app.",
  429: "Too many requests. Please wait a moment and try again.",
  500: "We can’t reach the payment service right now. Please try again in a minute.",
  502: "We can’t reach the payment service right now. Please try again in a minute.",
  503: "Payments are briefly unavailable. Please try again in a minute.",
};

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api/pay/${path}`, {
      ...init,
      cache: "no-store",
      headers: init?.body ? { "Content-Type": "application/json" } : undefined,
    });
  } catch {
    throw new PayError(0, "We can’t reach the payment service. Check your connection and try again.");
  }
  if (res.ok) return res.json() as Promise<T>;

  const body = await res.json().catch(() => null);
  const retryAfter = Number(res.headers.get("Retry-After"));
  throw new PayError(
    res.status,
    typeof body?.detail === "string"
      ? body.detail
      : (FALLBACK[res.status] ?? `Something went wrong (${res.status}). Please try again.`),
    Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : null,
    typeof body?.error === "string" ? body.error : null,
    typeof body?.utr_attempts_left === "number" ? body.utr_attempts_left : null,
  );
}

export const pay = {
  invoice: (token: string) => call<PayInvoice>(encodeURIComponent(token)),
  status: (token: string) => call<PayStatus>(`${encodeURIComponent(token)}/status`),
  submitUtr: (token: string, utr: string) =>
    call<UtrResult>(`${encodeURIComponent(token)}/utr`, { method: "POST", body: JSON.stringify({ utr }) }),
};
