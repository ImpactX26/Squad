import type { components } from "@/lib/api-types";
import { env } from "@/lib/env";

export type LoginRequest = components["schemas"]["LoginRequest"];
export type LoginResponse = components["schemas"]["LoginResponse"];
export type StaffUser = components["schemas"]["StaffUserOut"];
export type StaffRole = StaffUser["role"];

export type Ticket = components["schemas"]["TicketListItem"];
export type TicketList = components["schemas"]["TicketListResponse"];
export type TicketStatus = Ticket["status"];
export type TicketPriority = Ticket["priority"];
export type TicketCategory = Ticket["category"];
export type SourceChannel = Ticket["source_channel"];

export type TicketDetail = components["schemas"]["TicketDetail"];
export type TicketProduct = NonNullable<Ticket["product"]>;
export type Timeline = components["schemas"]["TimelineResponse"];
export type TimelineEntry = components["schemas"]["TimelineEntry"];
export type DiagnosticStep = components["schemas"]["DiagnosticStepOut"];
export type DiagnosticResult = DiagnosticStep["result"];
export type TicketPatch = Omit<components["schemas"]["TicketPatch"], "unassign"> & { unassign?: boolean };
export type PolishResult = components["schemas"]["PolishResponse"];
export type SendMessageBody = components["schemas"]["SendMessageRequest"];
export type SendMessageResult = components["schemas"]["SendMessageResponse"];
export type TicketPayment = NonNullable<TicketDetail["payment"]>;
export type MarkPaidResult = components["schemas"]["MarkPaidResponse"];

export type PaymentRow = components["schemas"]["PaymentRow"];
export type PaymentList = components["schemas"]["PaymentListResponse"];
export type PaymentDetail = components["schemas"]["PaymentDetail"];
export type PaymentCreate = components["schemas"]["PaymentCreate"];
export type PaymentCreated = components["schemas"]["PaymentCreated"];
export type PaymentPatch = components["schemas"]["PaymentPatch"];
export type BankAlert = components["schemas"]["BankAlertOut"];
export type BankAlertCreate = components["schemas"]["BankAlertCreate"];
export type BankAlertResult = components["schemas"]["BankAlertResult"];

/** The payments page's filters (§11.2). */
export type PaymentFilters = { status?: string; needs_review?: boolean; q?: string; limit?: number; offset?: number };

export type Job = components["schemas"]["JobOut"];
export type JobList = components["schemas"]["JobListResponse"];
export type JobPatch = components["schemas"]["JobPatch"];
export type JobStatus = Job["status"];

/** The §10 inbox filters. Array values repeat the query parameter, which is how FastAPI reads them. */
export type TicketFilters = {
  status?: TicketStatus[];
  priority?: TicketPriority[];
  channel?: SourceChannel[];
  category?: TicketCategory[];
  assignee?: string;
  open_only?: boolean;
  limit?: number;
  offset?: number;
};

function query(filters: TicketFilters | PaymentFilters): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value === undefined || value === null) continue;
    if (Array.isArray(value)) value.forEach((v) => params.append(key, String(v)));
    else params.set(key, String(value));
  }
  const text = params.toString();
  return text ? `?${text}` : "";
}

const TOKEN_KEY = "servicemesh.token";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

// "Keep me signed in" (the sign-in form): kept in localStorage, the token outlives the browser; not
// kept, it lives in sessionStorage and goes when the tab closes. Either way it expires on the server
// after JWT_EXPIRE_MINUTES.
export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY) ?? window.sessionStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string, keep = true): void {
  clearToken();
  (keep ? window.localStorage : window.sessionStorage).setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
  window.sessionStorage.removeItem(TOKEN_KEY);
}

/** fetch with the staff token, for the few calls that read a stream instead of JSON. */
export async function authorizedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  try {
    return await fetch(`${env.apiUrl}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check that the backend is running.");
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let res: Response;
  try {
    res = await fetch(`${env.apiUrl}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(0, "Can't reach the server. Check that the backend is running.");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    const detail = typeof body?.detail === "string" ? body.detail : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;  // DELETE: no body
  return res.json() as Promise<T>;
}

export const api = {
  login: (body: LoginRequest) =>
    request<LoginResponse>("/api/auth/login", { method: "POST", body: JSON.stringify(body) }),
  me: () => request<StaffUser>("/api/me"),
  tickets: (filters: TicketFilters = {}) => request<TicketList>(`/api/tickets${query(filters)}`),
  ticket: (id: string) => request<TicketDetail>(`/api/tickets/${id}`),
  timeline: (id: string) => request<Timeline>(`/api/tickets/${id}/timeline`),
  patchTicket: (id: string, body: TicketPatch) =>
    request<TicketDetail>(`/api/tickets/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteTicket: (id: string) => request<void>(`/api/tickets/${id}`, { method: "DELETE" }),
  polish: (id: string, text: string) =>
    request<PolishResult>(`/api/tickets/${id}/polish`, { method: "POST", body: JSON.stringify({ text }) }),
  sendMessage: (id: string, body: SendMessageBody) =>
    request<SendMessageResult>(`/api/tickets/${id}/messages`, { method: "POST", body: JSON.stringify(body) }),
  patchDiagnostic: (id: string, stepId: string, result: DiagnosticResult) =>
    request<DiagnosticStep>(`/api/tickets/${id}/diagnostics/${stepId}`, {
      method: "PATCH",
      body: JSON.stringify({ result }),
    }),
  myJobs: () => request<JobList>("/api/jobs/mine"),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  patchJob: (id: string, body: JobPatch) =>
    request<Job>(`/api/jobs/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  inventory: () => request<components["schemas"]["InventoryResponse"]>("/api/inventory"),
  restockRequests: () => request<components["schemas"]["RestockRequestList"]>("/api/restock-requests"),
  search: (query: string) =>
    request<components["schemas"]["SearchResponse"]>("/api/search", { method: "POST", body: JSON.stringify({ query }) }),
  suggestions: (ticketId: string) =>
    request<components["schemas"]["SuggestionsResponse"]>(`/api/tickets/${ticketId}/suggestions`),
  commands: () => request<components["schemas"]["CommandListResponse"]>("/api/commands"),
  createCommand: (body: components["schemas"]["CommandCreate"]) =>
    request<components["schemas"]["CommandOut"]>("/api/commands", { method: "POST", body: JSON.stringify(body) }),
  editCommand: (id: string, body: components["schemas"]["CommandPatch"]) =>
    request<components["schemas"]["CommandOut"]>(`/api/commands/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteCommand: (id: string) => request<void>(`/api/commands/${id}`, { method: "DELETE" }),
  markPaid: (paymentId: string, note: string) =>
    request<MarkPaidResult>(`/api/payments/${paymentId}/mark-paid`, {
      method: "POST",
      body: JSON.stringify({ note }),
    }),
  payments: (filters: PaymentFilters = {}) => request<PaymentList>(`/api/payments${query(filters)}`),
  payment: (id: string) => request<PaymentDetail>(`/api/payments/${id}`),
  createPayment: (body: PaymentCreate) =>
    request<PaymentCreated>("/api/payments", { method: "POST", body: JSON.stringify(body) }),
  patchPayment: (id: string, body: PaymentPatch) =>
    request<PaymentRow>(`/api/payments/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  rejectPayment: (id: string, note: string) =>
    request<PaymentRow>(`/api/payments/${id}/reject`, { method: "POST", body: JSON.stringify({ note }) }),
  cancelPayment: (id: string, note: string) =>
    request<PaymentRow>(`/api/payments/${id}/cancel`, { method: "POST", body: JSON.stringify({ note }) }),
  bankAlerts: () => request<components["schemas"]["BankAlertList"]>("/api/bank-alerts"),
  addBankAlert: (body: BankAlertCreate) =>
    request<BankAlertResult>("/api/bank-alerts", { method: "POST", body: JSON.stringify(body) }),
};

// §11.2: agents → /inbox, technicians → /jobs. Admin and warehouse aren't named there;
// they follow the "Who" column (/inventory is warehouse + admin, /inbox is agent).
export const HOME_BY_ROLE: Record<StaffRole, string> = {
  agent: "/inbox",
  admin: "/inbox",
  technician: "/jobs",
  warehouse: "/inventory",
};
