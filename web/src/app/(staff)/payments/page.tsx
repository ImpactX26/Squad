"use client";

// The payments page (ARCHITECTURE.md §7.6, §10, §11.2): every payment, by hand, beside the automation.
// Agents read; admins also act. The pay page's UTR and the bank-alert match stay the main path; every
// action here goes through the same payments MCP tools (or, for a pasted bank SMS, the same reader and
// matcher), and each one asks for a note that is kept with the admin's name. Nothing is deleted: a
// payment is cancelled. Refetched live when /ws/staff reports a payment or ticket change (§9).

import { BadgeIndianRupee, Inbox, LoaderCircle, MessageSquareText, Plus, X } from "lucide-react";
import Link from "next/link";
import { Dialog } from "radix-ui";
import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useState } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, errorMessage } from "@/components/states";
import { LiveBadge } from "@/components/ticket/live-badge";
import { Button } from "@/components/ui/button";
import {
  ApiError,
  type BankAlert,
  type BankAlertResult,
  type PaymentDetail,
  type PaymentList,
  type PaymentRow,
  api,
} from "@/lib/api";
import { absolute, relative } from "@/lib/format";
import { useStaffUser } from "@/lib/session";
import { type SocketStatus, type StaffEvent, openStaffSocket } from "@/lib/ws";

type Tab = "payments" | "alerts";
type Tone = "warning" | "accent" | "success" | "danger" | "muted";

const STATUS: Record<string, { label: string; tone: Tone }> = {
  pending: { label: "Unpaid", tone: "accent" },
  verifying: { label: "Checking", tone: "warning" },
  paid: { label: "Paid", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  expired: { label: "Expired", tone: "muted" },
  cancelled: { label: "Cancelled", tone: "muted" },
  refunded: { label: "Refunded", tone: "muted" },
};
const STATUS_FILTERS = ["", "pending", "verifying", "paid", "failed", "expired", "cancelled"];

// Events after which a payment may have changed (§9).
const PAYMENT_EVENTS = new Set<StaffEvent["type"]>([
  "payment.link_sent",
  "payment.utr_submitted",
  "payment.paid",
  "payment.failed",
  "ticket.updated",
]);

const EVENT_LABEL: Record<string, string> = {
  payment_link_created: "Link created",
  payment_utr_submitted: "UTR submitted",
  payment_paid: "Paid",
  payment_failed: "Failed",
  payment_cancelled: "Cancelled",
  payment_expired: "Link expired",
  payment_needs_review: "Flagged for review",
  payment_link_extended: "Link extended",
  payment_alert_unapplied: "Bank alert not applied",
  payment_confirmed: "Receipt sent",
};

const EXTEND_OPTIONS: [number, string][] = [
  [30, "30 minutes"],
  [60, "1 hour"],
  [120, "2 hours"],
  [1440, "1 day"],
  [4320, "3 days"],
  [10080, "7 days"],
];

const TH = "px-3 py-2 text-left text-footnote font-medium text-ink-secondary";
const TD = "px-3 py-2.5 text-subheadline text-ink";
const FIELD =
  "w-full rounded-control bg-canvas px-3 py-2 text-subheadline text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised";
const NOTE_MIN = 3;

const inr = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
const money = (amount: string | null | undefined) => (amount ? inr.format(Number(amount)) : "—");

export default function PaymentsPage() {
  const user = useStaffUser();
  const allowed = user?.role === "agent" || user?.role === "admin";
  const isAdmin = user?.role === "admin";
  const [tab, setTab] = useState<Tab>("payments");
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const [revision, setRevision] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [services, setServices] = useState<PaymentList["services"]>([]);

  useEffect(() => {
    if (!allowed) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const close = openStaffSocket((event) => {
      if (!PAYMENT_EVENTS.has(event.type)) return;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => setRevision((n) => n + 1), 300);
    }, setSocket);
    return () => {
      if (timer) clearTimeout(timer);
      close();
    };
  }, [allowed]);

  const changed = useCallback(() => setRevision((n) => n + 1), []);

  if (!allowed) return <ErrorState message="Payments are for agents and admins." />;

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-3">
        <h1 className="text-title-1 font-semibold tracking-tight text-ink">Payments</h1>
        <LiveBadge status={socket} />
        {isAdmin ? (
          <div className="flex w-full flex-wrap gap-2 sm:ml-auto sm:w-auto">
            <Button onClick={() => setCreating(true)} className="h-9 rounded-control text-[15px]">
              <Plus aria-hidden />
              New payment request
            </Button>
            <Button variant="outline" onClick={() => setPasting(true)} className="h-9 rounded-control text-[15px]">
              <MessageSquareText aria-hidden />
              Add bank SMS
            </Button>
          </div>
        ) : (
          <p className="text-footnote text-ink-secondary sm:ml-auto">Read-only: admins change payments.</p>
        )}
      </header>

      <div role="tablist" aria-label="Payments views" className="inline-flex rounded-control bg-surface p-1">
        {(["payments", "alerts"] as const).map((value) => (
          <button
            key={value}
            role="tab"
            type="button"
            aria-selected={tab === value}
            onClick={() => setTab(value)}
            className={`h-8 rounded-[8px] px-3 text-subheadline outline-none focus-visible:ring-2 focus-visible:ring-accent ${
              tab === value ? "bg-canvas font-medium text-ink dark:bg-surface-raised" : "text-ink-secondary hover:text-ink"
            }`}
          >
            {value === "payments" ? "Payments" : "Bank alerts"}
          </button>
        ))}
      </div>

      {tab === "payments" ? (
        <PaymentsTab revision={revision} onOpen={setSelected} onServices={setServices} />
      ) : (
        <AlertsTab revision={revision} onOpen={setSelected} />
      )}

      {/* Keyed on the payment, so opening another one starts from a clean drawer. */}
      <PaymentDrawer key={selected ?? "none"} id={selected} revision={revision} isAdmin={isAdmin} onClose={() => setSelected(null)} onChanged={changed} />
      {isAdmin ? (
        <>
          <NewPaymentDialog
            open={creating}
            services={services}
            onOpenChange={setCreating}
            onCreated={(id) => {
              changed();
              setSelected(id);
            }}
          />
          <BankSmsDialog
            open={pasting}
            onOpenChange={setPasting}
            onAdded={changed}
            onOpenPayment={(id) => {
              setPasting(false); // one dialog at a time: the drawer replaces this form
              setSelected(id);
            }}
          />
        </>
      ) : null}
    </div>
  );
}

// ---------- the list ----------

function PaymentsTab({
  revision,
  onOpen,
  onServices,
}: {
  revision: number;
  onOpen: (id: string) => void;
  onServices: (services: PaymentList["services"]) => void;
}) {
  const [status, setStatus] = useState("");
  const [review, setReview] = useState(false);
  const [words, setWords] = useState("");
  const [q, setQ] = useState("");
  const [load, setLoad] = useState<{ state: "loading" } | { state: "error"; message: string } | { state: "ready"; list: PaymentList }>(
    { state: "loading" },
  );
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    const timer = setTimeout(() => setQ(words.trim()), 300);
    return () => clearTimeout(timer);
  }, [words]);

  useEffect(() => {
    let cancelled = false;
    api
      .payments({ status: status || undefined, needs_review: review ? true : undefined, q: q || undefined, limit: 100 })
      .then((list) => {
        if (cancelled) return;
        setLoad({ state: "ready", list });
        setRefreshError(null);
        onServices(list.services);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = errorMessage(err, "Couldn’t load the payments.");
        setLoad((current) => (current.state === "ready" ? current : { state: "error", message }));
        setRefreshError(message);
      });
    return () => {
      cancelled = true;
    };
  }, [status, review, q, revision, retry, onServices]);

  return (
    <section aria-label="Payments" className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={words}
          onChange={(e) => setWords(e.target.value)}
          placeholder="Invoice, UTR, ticket or customer"
          aria-label="Search payments"
          className="h-9 min-w-0 basis-full rounded-control sm:max-w-xs sm:flex-1 sm:basis-0 bg-surface px-3 text-subheadline text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent"
        />
        <select
          value={status}
          onChange={(e) => setStatus(e.target.value)}
          aria-label="Status"
          className="h-9 rounded-control bg-surface px-2 text-subheadline text-ink outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          {STATUS_FILTERS.map((value) => (
            <option key={value} value={value}>
              {value ? STATUS[value].label : "Every status"}
            </option>
          ))}
        </select>
        <label className="inline-flex items-center gap-2 text-subheadline text-ink-secondary">
          <input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} className="accent-accent" />
          Needs review
        </label>
        {load.state === "ready" ? (
          <span className="text-footnote tabular-nums text-ink-secondary sm:ml-auto">
            {load.list.total} payment{load.list.total === 1 ? "" : "s"}
          </span>
        ) : null}
      </div>

      {load.state === "loading" ? <LoadingState label="Loading payments" rows={6} rowClassName="h-11" /> : null}
      {load.state === "error" ? <ErrorState message={load.message} onRetry={() => setRetry((n) => n + 1)} /> : null}
      {load.state === "ready" ? (
        <>
          {refreshError ? <InlineError message={refreshError} onRetry={() => setRetry((n) => n + 1)} /> : null}
          {load.list.payments.length === 0 ? (
            <EmptyState
              icon={BadgeIndianRupee}
              title={status || review || q ? "No payment matches these filters." : "No payments yet."}
              hint="A payment appears when /payments (or an admin here) sends a customer an invoice link."
            />
          ) : (
            <div className="overflow-x-auto rounded-card bg-surface">
              <table className="w-full min-w-[68rem]">
                <thead className="border-b border-hairline">
                  <tr>
                    <th className={TH}>Invoice</th>
                    <th className={TH}>Ticket</th>
                    <th className={TH}>Customer</th>
                    <th className={TH}>Service</th>
                    <th className={`${TH} text-right`}>Amount</th>
                    <th className={TH}>Status</th>
                    <th className={TH}>UTR</th>
                    <th className={`${TH} text-right`}>Tries left</th>
                    <th className={TH}>Verified by</th>
                    <th className={TH}>Created</th>
                    <th className={TH}>Paid</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-hairline">
                  {load.list.payments.map((p) => (
                    <tr key={p.payment_id} className="cursor-pointer hover:bg-canvas/60 dark:hover:bg-surface-raised/60" onClick={() => onOpen(p.payment_id)}>
                      <td className={TD}>
                        <button
                          type="button"
                          onClick={(e) => {
                            e.stopPropagation();
                            onOpen(p.payment_id);
                          }}
                          className="rounded-sm font-medium tabular-nums text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
                        >
                          {p.invoice_number ?? "—"}
                        </button>
                      </td>
                      <td className={`${TD} tabular-nums`}>{p.ticket_number}</td>
                      <td className={TD}>{p.customer_name ?? "—"}</td>
                      <td className={`${TD} text-ink-secondary`}>{p.service_name ?? p.service_code}</td>
                      <td className={`${TD} text-right tabular-nums`}>{money(p.amount)}</td>
                      <td className={TD}>
                        <span className="inline-flex flex-wrap gap-1">
                          <StatusPill status={p.status} />
                          {p.needs_review ? <Pill tone="warning">Needs review</Pill> : null}
                        </span>
                      </td>
                      <td className={`${TD} tabular-nums`}>{p.utr ?? "—"}</td>
                      <td className={`${TD} text-right tabular-nums`}>{p.utr_attempts_left}</td>
                      <td className={`${TD} text-ink-secondary`}>{p.verified_by_label ?? "—"}</td>
                      <td className={`${TD} text-ink-secondary`} title={absolute(p.created_at)}>{relative(p.created_at)}</td>
                      <td className={`${TD} text-ink-secondary`} title={p.paid_at ? absolute(p.paid_at) : undefined}>
                        {p.paid_at ? relative(p.paid_at) : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : null}
    </section>
  );
}

// ---------- bank alerts ----------

function AlertsTab({ revision, onOpen }: { revision: number; onOpen: (id: string) => void }) {
  const [load, setLoad] = useState<{ state: "loading" } | { state: "error"; message: string } | { state: "ready"; alerts: BankAlert[] }>(
    { state: "loading" },
  );
  const [retry, setRetry] = useState(0);

  useEffect(() => {
    let cancelled = false;
    api
      .bankAlerts()
      .then(({ alerts }) => !cancelled && setLoad({ state: "ready", alerts }))
      .catch((err: unknown) => {
        if (!cancelled) setLoad((c) => (c.state === "ready" ? c : { state: "error", message: errorMessage(err, "Couldn’t load the bank alerts.") }));
      });
    return () => {
      cancelled = true;
    };
  }, [revision, retry]);

  if (load.state === "loading") return <LoadingState label="Loading bank alerts" rows={5} rowClassName="h-11" />;
  if (load.state === "error") return <ErrorState message={load.message} onRetry={() => setRetry((n) => n + 1)} />;
  if (load.alerts.length === 0) {
    return (
      <EmptyState
        icon={Inbox}
        title="No bank alerts yet."
        hint="Each credit SMS the phone forwards (or an admin pastes) is stored here, without its text."
      />
    );
  }
  return (
    <section aria-label="Bank alerts" className="overflow-x-auto rounded-card bg-surface">
      <table className="w-full min-w-[56rem]">
        <thead className="border-b border-hairline">
          <tr>
            <th className={TH}>Received</th>
            <th className={TH}>From</th>
            <th className={TH}>UTR</th>
            <th className={`${TH} text-right`}>Amount</th>
            <th className={TH}>Read as</th>
            <th className={TH}>Matched payment</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-hairline">
          {load.alerts.map((a) => (
            <tr key={a.id}>
              <td className={`${TD} text-ink-secondary`} title={absolute(a.received_at)}>{relative(a.received_at)}</td>
              <td className={TD}>{a.added_by ? `Pasted by ${a.added_by}` : a.sender}</td>
              <td className={`${TD} tabular-nums`}>{a.utr ?? "—"}</td>
              <td className={`${TD} text-right tabular-nums`}>{money(a.amount)}</td>
              <td className={TD}>
                {a.parsed_ok && !a.reject_reason ? (
                  <Pill tone="success">Credit</Pill>
                ) : (
                  <span className="text-footnote text-danger">{a.reject_reason ?? "Not a credit"}</span>
                )}
              </td>
              <td className={TD}>
                {a.matched_payment_id ? (
                  <button
                    type="button"
                    onClick={() => onOpen(a.matched_payment_id!)}
                    className="rounded-sm tabular-nums text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
                  >
                    {a.matched_invoice_number} · {a.matched_ticket_number}
                  </button>
                ) : (
                  <span className="text-ink-secondary">{a.parsed_ok ? "Waiting for a UTR" : "—"}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

// ---------- the drawer ----------

type Action = "verify" | "utr" | "extend" | "reject" | "cancel";

function actionsFor(p: PaymentRow): Action[] {
  const out: Action[] = [];
  if (p.status === "verifying" || p.status === "failed") out.push("verify");
  if (["pending", "verifying", "failed"].includes(p.status) && p.utr_attempts_left > 0) out.push("utr");
  if (p.status === "pending" || p.status === "expired") out.push("extend");
  if (p.status === "verifying") out.push("reject");
  if (["pending", "verifying", "failed"].includes(p.status)) out.push("cancel");
  return out;
}

const ACTION_LABEL: Record<Action, string> = {
  verify: "Mark as paid",
  utr: "Correct UTR",
  extend: "Extend link",
  reject: "Reject",
  cancel: "Cancel payment",
};

function PaymentDrawer({
  id,
  revision,
  isAdmin,
  onClose,
  onChanged,
}: {
  id: string | null;
  revision: number;
  isAdmin: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<PaymentDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [action, setAction] = useState<Action | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    api
      .payment(id)
      .then((d) => {
        if (cancelled) return;
        setDetail(d);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError && err.status === 404 ? "No such payment." : errorMessage(err, "Couldn’t load this payment."));
      });
    return () => {
      cancelled = true;
    };
  }, [id, revision]);

  return (
    <Dialog.Root open={id !== null} onOpenChange={(open) => !open && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/25 animate-in fade-in" />
        <Dialog.Content className="fixed inset-y-0 right-0 z-50 flex w-full max-w-lg flex-col bg-surface-raised shadow-raised outline-none animate-in slide-in-from-right-4">
          <div className="flex items-center gap-3 border-b border-hairline px-5 py-3">
            <Dialog.Title className="min-w-0 flex-1 truncate text-title-2 font-semibold tracking-tight text-ink">
              {detail?.invoice_number ?? "Payment"}
            </Dialog.Title>
            {detail ? <StatusPill status={detail.status} /> : null}
            <Dialog.Close aria-label="Close" className="grid size-8 place-items-center rounded-full text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent">
              <X className="size-4" aria-hidden />
            </Dialog.Close>
          </div>
          <Dialog.Description className="sr-only">The payment, its timeline and its bank alerts.</Dialog.Description>
          <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4">
            {error ? <ErrorState message={error} /> : null}
            {!detail && !error ? (
              // The drawer is the raised surface, so its placeholder blocks use the canvas tone.
              <div className="space-y-3" aria-busy="true" aria-label="Loading payment">
                {[0, 1, 2, 3].map((i) => (
                  <div key={i} className="h-14 rounded-card bg-canvas dark:bg-surface" />
                ))}
              </div>
            ) : null}
            {detail ? (
              <>
                <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-subheadline">
                  <dt className="text-ink-secondary">Ticket</dt>
                  <dd>
                    <Link href={`/tickets/${detail.ticket_id}`} prefetch={false} className="tabular-nums text-accent hover:underline">
                      {detail.ticket_number}
                    </Link>
                  </dd>
                  <dt className="text-ink-secondary">Customer</dt>
                  <dd className="text-ink">
                    {detail.customer_name ?? "—"}
                    {detail.customer_email ? <span className="block break-all text-ink-secondary">{detail.customer_email}</span> : null}
                  </dd>
                  <dt className="text-ink-secondary">Service</dt>
                  <dd className="text-ink">{detail.service_name ?? detail.service_code}</dd>
                  <dt className="text-ink-secondary">UTR</dt>
                  <dd className="tabular-nums text-ink">
                    {detail.utr ?? "—"}{" "}
                    <span className="text-ink-secondary">({detail.utr_attempts_left} of 5 tries left)</span>
                  </dd>
                  <dt className="text-ink-secondary">Verified by</dt>
                  <dd className="text-ink">{detail.verified_by_label ?? "—"}</dd>
                  <dt className="text-ink-secondary">Link expires</dt>
                  <dd className="text-ink">{absolute(detail.expires_at)}</dd>
                  <dt className="text-ink-secondary">Created</dt>
                  <dd className="text-ink">{absolute(detail.created_at)}</dd>
                  {detail.paid_at ? (
                    <>
                      <dt className="text-ink-secondary">Paid</dt>
                      <dd className="text-ink">{absolute(detail.paid_at)}</dd>
                    </>
                  ) : null}
                </dl>
                {detail.needs_review ? (
                  <p role="status" className="rounded-card bg-warning/15 px-3 py-2 text-subheadline text-ink">
                    No bank alert arrived for this UTR in time. Check the bank statement, then mark it paid or reject it.
                  </p>
                ) : null}

                <ul aria-label="Line items" className="divide-y divide-hairline border-y border-hairline">
                  {detail.line_items.map((item, i) => (
                    <li key={`${i}-${item.label}`} className="flex justify-between gap-4 py-2 text-subheadline">
                      <span className="min-w-0 text-ink">{item.label}</span>
                      <span className="shrink-0 tabular-nums text-ink">{money(item.amount)}</span>
                    </li>
                  ))}
                  <li className="flex justify-between gap-4 py-2 text-subheadline font-semibold">
                    <span className="text-ink">Total</span>
                    <span className="tabular-nums text-ink">{money(detail.amount)}</span>
                  </li>
                </ul>

                {isAdmin ? (
                  <section aria-label="Actions" className="space-y-3">
                    {done ? (
                      <p role="status" className="rounded-card bg-success/12 px-3 py-2 text-subheadline text-ink">
                        {done}
                      </p>
                    ) : null}
                    {action ? (
                      <ActionForm
                        key={action}
                        action={action}
                        payment={detail}
                        onBack={() => setAction(null)}
                        onDone={(message) => {
                          setAction(null);
                          setDone(message);
                          onChanged();
                        }}
                      />
                    ) : actionsFor(detail).length ? (
                      <div className="flex flex-wrap gap-2">
                        {actionsFor(detail).map((a) => (
                          <Button
                            key={a}
                            variant={a === "verify" ? "default" : "outline"}
                            onClick={() => {
                              setDone(null);
                              setAction(a);
                            }}
                            className={`h-9 rounded-control text-[15px] ${a === "cancel" || a === "reject" ? "text-danger" : ""}`}
                          >
                            {a === "utr" && !detail.utr ? "Enter UTR" : ACTION_LABEL[a]}
                          </Button>
                        ))}
                      </div>
                    ) : (
                      <p className="text-footnote text-ink-secondary">
                        {detail.status === "paid"
                          ? "Paid. A paid payment can’t be cancelled here: that would be a refund."
                          : "Nothing left to change on this payment."}
                      </p>
                    )}
                  </section>
                ) : null}

                <Section title="Timeline">
                  {detail.events.length ? (
                    <ol className="space-y-2">
                      {detail.events.map((e) => (
                        <li key={e.id} className="text-subheadline">
                          <span className="font-medium text-ink">{EVENT_LABEL[e.type] ?? e.type.replace(/_/g, " ")}</span>
                          <span className="text-ink-secondary">
                            {" "}· {e.actor_name ?? (e.actor === "customer" ? "Customer" : "System")} ·{" "}
                            <time dateTime={e.created_at} title={absolute(e.created_at)}>{relative(e.created_at)}</time>
                          </span>
                          {eventDetail(e.payload) ? <p className="text-footnote text-ink-secondary">{eventDetail(e.payload)}</p> : null}
                        </li>
                      ))}
                    </ol>
                  ) : (
                    <p className="text-footnote text-ink-secondary">No events yet.</p>
                  )}
                </Section>

                <Section title="Bank alerts">
                  {detail.bank_alerts.length ? (
                    <ul className="space-y-2">
                      {detail.bank_alerts.map((a) => (
                        <li key={a.id} className="text-subheadline">
                          <span className="tabular-nums text-ink">
                            {a.utr ?? "no UTR"} · {money(a.amount)}
                          </span>
                          <span className="text-ink-secondary"> · {a.added_by ? `pasted by ${a.added_by}` : a.sender} · {relative(a.received_at)}</span>
                          {a.reject_reason ? <p className="text-footnote text-danger">{a.reject_reason}</p> : null}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-footnote text-ink-secondary">No bank alert for this payment’s UTR yet.</p>
                  )}
                </Section>
              </>
            ) : null}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function eventDetail(payload: Record<string, unknown>): string | null {
  for (const key of ["note", "reason"]) {
    if (typeof payload[key] === "string" && payload[key]) return payload[key] as string;
  }
  return typeof payload.utr === "string" ? `UTR ${payload.utr}` : null;
}

function ActionForm({
  action,
  payment,
  onBack,
  onDone,
}: {
  action: Action;
  payment: PaymentRow;
  onBack: () => void;
  onDone: (message: string) => void;
}) {
  const [note, setNote] = useState("");
  const [utr, setUtr] = useState("");
  const [minutes, setMinutes] = useState(60);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const noteId = useId();
  const utrId = useId();
  const ready = note.trim().length >= NOTE_MIN && (action !== "utr" || utr.length === 12);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    const text = note.trim();
    try {
      if (action === "verify") {
        await api.markPaid(payment.payment_id, text);
        onDone("Marked paid with your name. The receipt and the booking run as usual.");
      } else if (action === "utr") {
        const out = await api.patchPayment(payment.payment_id, { utr, note: text });
        onDone(out.status === "paid" ? `UTR ${utr} matched the bank alert: paid.` : `UTR set to ${utr}; checking with the bank.`);
      } else if (action === "extend") {
        await api.patchPayment(payment.payment_id, { expires_in_minutes: minutes, note: text });
        onDone("Link extended. The customer isn’t messaged: tell them the link works again.");
      } else if (action === "reject") {
        await api.rejectPayment(payment.payment_id, text);
        onDone("Rejected. The customer can send another UTR while tries are left.");
      } else {
        await api.cancelPayment(payment.payment_id, text);
        onDone("Cancelled. The ticket is back in progress; the record is kept.");
      }
    } catch (err: unknown) {
      setError(errorMessage(err, "That didn’t work."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={(e) => void submit(e)} className="space-y-3 rounded-card bg-canvas p-3 dark:bg-surface">
      <p className="text-subheadline font-medium text-ink">{action === "utr" && !payment.utr ? "Enter UTR" : ACTION_LABEL[action]}</p>
      {action === "utr" ? (
        <div className="space-y-1">
          <label htmlFor={utrId} className="text-footnote text-ink-secondary">
            The correct 12-digit UTR. It uses one of the customer’s {payment.utr_attempts_left} tries left.
          </label>
          <input
            id={utrId}
            value={utr}
            onChange={(e) => setUtr(e.target.value.replace(/\D/g, "").slice(0, 12))}
            inputMode="numeric"
            autoComplete="off"
            placeholder="12 digits"
            className={`${FIELD} h-10 tabular-nums`}
          />
        </div>
      ) : null}
      {action === "extend" ? (
        <label className="block space-y-1 text-footnote text-ink-secondary">
          Link works for
          <select value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} className={`${FIELD} h-10`}>
            {EXTEND_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label} from now
              </option>
            ))}
          </select>
        </label>
      ) : null}
      {action === "cancel" && payment.status === "verifying" ? (
        <p className="text-footnote text-warning">A UTR was submitted: money may be on its way. Check the bank statement first.</p>
      ) : null}
      <div className="space-y-1">
        <label htmlFor={noteId} className="text-footnote text-ink-secondary">
          {action === "reject" ? "Why? What did you check?" : "What did you check?"} Kept with your name.
        </label>
        <textarea
          id={noteId}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          rows={2}
          maxLength={500}
          required
          placeholder={action === "reject" ? "No such credit on the statement" : "Checked the bank statement"}
          className={`${FIELD} resize-y`}
        />
      </div>
      {error ? (
        <p role="alert" className="text-footnote text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button type="button" variant="outline" onClick={onBack} disabled={busy} className="h-9 rounded-control text-[15px]">
          Back
        </Button>
        <Button type="submit" disabled={!ready || busy} className="h-9 flex-1 rounded-control text-[15px]">
          {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
          {action === "utr" && !payment.utr ? "Enter UTR" : ACTION_LABEL[action]}
        </Button>
      </div>
    </form>
  );
}

// ---------- an admin's forms ----------

function FormDialog({
  open,
  onOpenChange,
  title,
  description,
  children,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-50 bg-black/25 animate-in fade-in" />
        <Dialog.Content className="fixed top-[6vh] left-1/2 z-50 max-h-[88vh] w-[min(32rem,calc(100vw-2rem))] -translate-x-1/2 overflow-y-auto rounded-card bg-surface-raised p-5 shadow-raised outline-none animate-in fade-in slide-in-from-top-2">
          <div className="mb-3 flex items-start gap-3">
            <div className="min-w-0 flex-1 space-y-1">
              <Dialog.Title className="text-title-2 font-semibold tracking-tight text-ink">{title}</Dialog.Title>
              <Dialog.Description className="text-footnote text-ink-secondary">{description}</Dialog.Description>
            </div>
            <Dialog.Close aria-label="Close" className="grid size-8 shrink-0 place-items-center rounded-full text-ink-secondary outline-none hover:text-ink focus-visible:ring-2 focus-visible:ring-accent">
              <X className="size-4" aria-hidden />
            </Dialog.Close>
          </div>
          {children}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function NewPaymentDialog({
  open,
  services,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  services: PaymentList["services"];
  onOpenChange: (open: boolean) => void;
  onCreated: (paymentId: string) => void;
}) {
  const [ticket, setTicket] = useState("");
  const [service, setService] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = ticket.trim().length > 3 && service && note.trim().length >= NOTE_MIN;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      const out = await api.createPayment({ ticket_number: ticket.trim(), service_code: service, note: note.trim() });
      onOpenChange(false);
      setTicket("");
      setService("");
      setNote("");
      onCreated(out.payment.payment_id);
    } catch (err: unknown) {
      setError(errorMessage(err, "Couldn’t create the payment request."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="New payment request"
      description="For a ticket whose customer has an address on file. The amount comes from the catalog; the customer gets the link on their chat and the invoice by email."
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <label className="block space-y-1 text-footnote text-ink-secondary">
          Ticket number
          <input value={ticket} onChange={(e) => setTicket(e.target.value)} placeholder="SR-2026-00042" className={`${FIELD} h-10 tabular-nums`} />
        </label>
        <label className="block space-y-1 text-footnote text-ink-secondary">
          Service
          <select value={service} onChange={(e) => setService(e.target.value)} className={`${FIELD} h-10`}>
            <option value="">Choose a service</option>
            {services.map((s) => (
              <option key={s.code} value={s.code}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label className="block space-y-1 text-footnote text-ink-secondary">
          What did you check? Kept with your name.
          <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={500} placeholder="Customer agreed the price on the phone" className={`${FIELD} resize-y`} />
        </label>
        {error ? (
          <p role="alert" className="text-footnote text-danger">
            {error}
          </p>
        ) : null}
        <Button type="submit" disabled={!ready || busy} className="h-10 w-full rounded-control text-[15px]">
          {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
          Create and send the link
        </Button>
      </form>
    </FormDialog>
  );
}

const MATCH_TEXT: Record<string, (r: BankAlertResult) => string> = {
  paid: (r) => `Matched ${r.invoice_number} and marked it paid, verified by you.`,
  amount_mismatch: (r) => `The UTR is ${r.invoice_number}’s, but the amount differs: it was marked failed for review.`,
  waiting_for_utr: () => "Stored. No payment holds this UTR yet; it matches when the customer submits it.",
  duplicate: (r) => `${r.invoice_number} was already paid; nothing changed.`,
  not_verifying: (r) => `${r.invoice_number} isn’t waiting for verification, so nothing changed.`,
};

function BankSmsDialog({
  open,
  onOpenChange,
  onAdded,
  onOpenPayment,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onAdded: () => void;
  onOpenPayment: (id: string) => void;
}) {
  const [sms, setSms] = useState("");
  const [sender, setSender] = useState("");
  const [subject, setSubject] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BankAlertResult | null>(null);
  const ready = sms.trim().length >= 10 && note.trim().length >= NOTE_MIN;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!ready) return;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const out = await api.addBankAlert({ sms, sender: sender.trim() || null, subject: subject.trim() || null, note: note.trim() });
      setResult(out);
      onAdded();
      if (out.parsed_ok) setSms("");
    } catch (err: unknown) {
      setError(errorMessage(err, "Couldn’t add the SMS."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <FormDialog
      open={open}
      onOpenChange={(next) => {
        onOpenChange(next);
        if (!next) setResult(null);
      }}
      title="Add bank SMS"
      description="Paste the bank’s credit SMS as you received it. It is read and matched exactly like a forwarded one; a match is recorded as your verification. The SMS text isn’t stored."
    >
      <form onSubmit={(e) => void submit(e)} className="space-y-3">
        <label className="block space-y-1 text-footnote text-ink-secondary">
          SMS
          <textarea value={sms} onChange={(e) => setSms(e.target.value)} rows={4} maxLength={2000} placeholder="Rs.6.90 credited to your A/c XX4321 … UPI Ref No 427512345678 …" className={`${FIELD} resize-y`} />
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block space-y-1 text-footnote text-ink-secondary">
            Sender (optional)
            <input value={sender} onChange={(e) => setSender(e.target.value)} placeholder="VM-HDFCBK" className={`${FIELD} h-10`} />
          </label>
          <label className="block space-y-1 text-footnote text-ink-secondary">
            Subject (optional)
            <input value={subject} onChange={(e) => setSubject(e.target.value)} placeholder="UPI-Verify" className={`${FIELD} h-10`} />
          </label>
        </div>
        <label className="block space-y-1 text-footnote text-ink-secondary">
          What did you check? Kept with your name.
          <textarea value={note} onChange={(e) => setNote(e.target.value)} rows={2} maxLength={500} placeholder="SMS seen on the company phone" className={`${FIELD} resize-y`} />
        </label>
        {error ? (
          <p role="alert" className="text-footnote text-danger">
            {error}
          </p>
        ) : null}
        {result ? (
          <div role="status" className={`rounded-card px-3 py-2 text-subheadline text-ink ${result.parsed_ok ? "bg-success/12" : "bg-danger/12"}`}>
            {result.parsed_ok ? (
              <>
                <p className="tabular-nums">
                  Read UTR {result.utr}, {money(result.amount)}.
                </p>
                <p>{result.match ? (MATCH_TEXT[result.match]?.(result) ?? result.match) : ""}</p>
                {result.payment_id ? (
                  <button type="button" onClick={() => onOpenPayment(result.payment_id!)} className="text-accent hover:underline">
                    Open {result.invoice_number}
                  </button>
                ) : null}
              </>
            ) : (
              <p>Not read as a credit ({result.reject_reason}). Nothing was paid; it’s listed under Bank alerts.</p>
            )}
          </div>
        ) : null}
        <Button type="submit" disabled={!ready || busy} className="h-10 w-full rounded-control text-[15px]">
          {busy ? <LoaderCircle className="animate-spin" aria-hidden /> : null}
          Read and match
        </Button>
      </form>
    </FormDialog>
  );
}

// ---------- small pieces ----------

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="space-y-2">
      <h2 className="text-subheadline font-semibold text-ink">{title}</h2>
      {children}
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  const s = STATUS[status] ?? { label: status, tone: "muted" as Tone };
  return <Pill tone={s.tone}>{s.label}</Pill>;
}

function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  const tones: Record<Tone, string> = {
    warning: "bg-warning/15 text-warning",
    accent: "bg-accent/12 text-accent",
    success: "bg-success/15 text-success",
    danger: "bg-danger/12 text-danger",
    muted: "bg-canvas text-ink-secondary dark:bg-surface-raised",
  };
  return (
    <span className={`inline-flex whitespace-nowrap rounded-full px-2.5 py-0.5 text-footnote font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}
