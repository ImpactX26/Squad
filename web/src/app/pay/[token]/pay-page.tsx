"use client";

// The customer's checkout page, /pay/[token] (ARCHITECTURE.md sections 7.6, 11.2, 11.3): the invoice,
// a UPI QR code, and the UTR form. The QR encodes upi.uri exactly as GET /api/pay/{token} sends it;
// the payment link is never rebuilt in the browser. While a UTR is being checked, the page polls
// GET /api/pay/{token}/status every 3 s and stops when the tab is hidden or the status is final.

import { Check, CircleCheck, CircleX, Clock, Copy, Link2Off, LoaderCircle, TriangleAlert } from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { type FormEvent, type ReactNode, useCallback, useEffect, useId, useRef, useState } from "react";

import { PayError, type PayInvoice, type PayStatus, type UtrResult, pay } from "@/lib/pay";

const POLL_MS = 3000;
const UTR_DIGITS = 12;
const HELP = "Questions about this invoice? Reply in the chat where you got this link.";

type Load =
  | { state: "loading" }
  | { state: "not_found" }
  | { state: "error"; message: string }
  | { state: "ready"; invoice: PayInvoice };

type Patch = Partial<Pick<PayInvoice, "status" | "message" | "utr" | "utr_attempts_left" | "paid_at" | "paid_display">>;

const MUTED = "bg-canvas text-ink-secondary dark:bg-surface-raised";
const PILL: Record<string, { label: string; tint: string }> = {
  pending: { label: "Unpaid", tint: "bg-accent/12 text-accent" },
  verifying: { label: "Checking", tint: "bg-warning/15 text-warning" },
  paid: { label: "Paid", tint: "bg-success/15 text-success" },
  failed: { label: "Not matched", tint: "bg-danger/15 text-danger" },
  expired: { label: "Expired", tint: MUTED },
  cancelled: { label: "Cancelled", tint: MUTED },
  refunded: { label: "Refunded", tint: MUTED },
};

function fromStatus(status: PayStatus): Patch {
  return {
    status: status.status,
    message: status.message,
    utr_attempts_left: status.utr_attempts_left,
    paid_at: status.paid_at,
    paid_display: status.paid_display,
  };
}

export function PayPage({ token, companyName }: { token: string; companyName: string }) {
  const [load, setLoad] = useState<Load>({ state: "loading" });

  const loadInvoice = useCallback(
    (): Promise<Load> =>
      pay.invoice(token).then(
        (invoice) => ({ state: "ready", invoice }),
        (err: unknown) =>
          err instanceof PayError && err.status === 404
            ? { state: "not_found" }
            : { state: "error", message: err instanceof Error ? err.message : "Couldn’t load this invoice." },
      ),
    [token],
  );

  useEffect(() => {
    let live = true;
    void loadInvoice().then((next) => {
      if (live) setLoad(next);
    });
    return () => {
      live = false;
    };
  }, [loadInvoice]);

  const apply = useCallback((patch: Patch) => {
    setLoad((current) =>
      current.state === "ready" ? { state: "ready", invoice: { ...current.invoice, ...patch } } : current,
    );
  }, []);

  /** Ask the server where the payment stands now, and show that. A failed check changes nothing. */
  const recheck = useCallback(async () => {
    try {
      apply(fromStatus(await pay.status(token)));
    } catch (err: unknown) {
      if (err instanceof PayError && err.status === 404) setLoad({ state: "not_found" });
    }
  }, [apply, token]);

  const status = load.state === "ready" ? load.invoice.status : null;
  const expiresAt = load.state === "ready" ? load.invoice.expires_at : null;

  // Poll while the UTR is checked against the bank's alert. One request at a time, none while the
  // tab is hidden (it resumes when the tab is shown again), and a 429 waits out its Retry-After.
  useEffect(() => {
    if (status !== "verifying") return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let inFlight = false;
    let stopped = false;

    async function poll() {
      if (stopped || inFlight || document.visibilityState !== "visible") return;
      inFlight = true;
      let wait = POLL_MS;
      try {
        const next = await pay.status(token);
        if (stopped) return;
        apply(fromStatus(next)); // a new status re-runs this effect, which ends this loop
        if (next.status !== "verifying") return;
      } catch (err: unknown) {
        if (stopped) return;
        if (err instanceof PayError && err.status === 404) {
          setLoad({ state: "not_found" });
          return;
        }
        if (err instanceof PayError && err.retryAfter) wait = Math.max(POLL_MS, err.retryAfter * 1000);
      } finally {
        inFlight = false;
      }
      timer = setTimeout(() => void poll(), wait);
    }

    function onVisibility() {
      clearTimeout(timer);
      if (document.visibilityState === "visible") void poll();
    }

    timer = setTimeout(() => void poll(), POLL_MS);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stopped = true;
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [status, token, apply]);

  // An unpaid link turns into "expired" at expires_at without a reload: one check at that moment,
  // and again every 15 s if the server's clock hasn't got there yet.
  useEffect(() => {
    if (status !== "pending" || !expiresAt) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = () => {
      void recheck();
      timer = setTimeout(check, 15_000);
    };
    const due = new Date(expiresAt).getTime() - Date.now() + 1000;
    timer = setTimeout(check, Math.min(Math.max(due, 0), 2 ** 31 - 1));
    return () => clearTimeout(timer);
  }, [status, expiresAt, recheck]);

  const onSubmitted = useCallback(
    (utr: string, result: UtrResult) => {
      apply({ status: result.status, message: result.message, utr, utr_attempts_left: result.utr_attempts_left });
      if (result.status === "paid") void recheck(); // for the time it was paid
    },
    [apply, recheck],
  );

  const onRefused = useCallback(
    (err: PayError) => {
      if (err.status === 404) setLoad({ state: "not_found" });
      else if (err.status === 410 || err.status === 409) void recheck(); // expired, paid, cancelled, used
      else if (err.code === "too_many_attempts") apply({ utr_attempts_left: 0 });
      if (err.attemptsLeft !== null) apply({ utr_attempts_left: err.attemptsLeft });
    },
    [apply, recheck],
  );

  if (load.state === "loading") {
    return (
      <Shell companyName={companyName}>
        <div aria-busy="true" aria-label="Loading your invoice" className="space-y-4">
          <div className="h-72 animate-pulse rounded-panel bg-surface" />
          <div className="h-96 animate-pulse rounded-panel bg-surface" />
        </div>
      </Shell>
    );
  }

  if (load.state === "not_found") {
    return (
      <Shell companyName={companyName} help={false}>
        <Notice icon={Link2Off} tint="text-ink-secondary" title="This payment link isn’t valid" heading="h1">
          <p>Check that you opened the whole link, or ask us for a new one in the chat where you got it.</p>
        </Notice>
      </Shell>
    );
  }

  if (load.state === "error") {
    return (
      <Shell companyName={companyName}>
        <Notice icon={TriangleAlert} tint="text-warning" title="We couldn’t load your invoice" heading="h1">
          <p>{load.message}</p>
          <button
            type="button"
            onClick={() => {
              setLoad({ state: "loading" });
              void loadInvoice().then(setLoad);
            }}
            className="mt-1 h-11 rounded-control bg-accent px-5 text-subheadline font-semibold text-on-accent"
          >
            Try again
          </button>
        </Notice>
      </Shell>
    );
  }

  const { invoice } = load;
  return (
    <Shell companyName={companyName}>
      <InvoiceCard invoice={invoice} />
      <div aria-live="polite" className="space-y-4">
        <PaymentState invoice={invoice} token={token} onSubmitted={onSubmitted} onRefused={onRefused} />
      </div>
    </Shell>
  );
}

// ---------- layout ----------

function Shell({ companyName, help = true, children }: { companyName: string; help?: boolean; children: ReactNode }) {
  return (
    <main className="mx-auto w-full max-w-md space-y-4 px-4 pb-10 pt-6">
      <p className="px-1 text-subheadline font-semibold tracking-tight text-ink">{companyName}</p>
      {children}
      {help ? <p className="px-1 text-footnote text-ink-secondary">{HELP}</p> : null}
    </main>
  );
}

function Panel({ label, children, className = "" }: { label?: string; children: ReactNode; className?: string }) {
  return (
    <section aria-label={label} className={`space-y-4 rounded-panel bg-surface p-5 ${className}`}>
      {children}
    </section>
  );
}

function Notice({
  icon: Icon,
  tint,
  title,
  heading = "h2",
  children,
}: {
  icon: typeof Clock;
  tint: string;
  title: string;
  heading?: "h1" | "h2";
  children: ReactNode;
}) {
  const Heading = heading;
  return (
    <Panel>
      <div className="flex items-start gap-3">
        <Icon className={`mt-0.5 size-6 shrink-0 ${tint}`} aria-hidden />
        <Heading className="text-title-2 font-semibold tracking-tight text-ink">{title}</Heading>
      </div>
      <div className="space-y-2 text-subheadline text-ink-secondary">{children}</div>
    </Panel>
  );
}

// ---------- the invoice ----------

function InvoiceCard({ invoice }: { invoice: PayInvoice }) {
  const pill = PILL[invoice.status] ?? { label: invoice.status, tint: MUTED };
  return (
    <Panel label="Invoice">
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
        <h1 className="text-title-2 font-semibold tracking-tight text-ink">
          Invoice <span className="tabular-nums">{invoice.invoice_number}</span>
        </h1>
        <span className={`rounded-full px-2.5 py-0.5 text-footnote font-medium ${pill.tint}`}>{pill.label}</span>
      </div>

      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1.5 text-subheadline">
        <dt className="text-ink-secondary">Ticket</dt>
        <dd className="tabular-nums text-ink">{invoice.ticket_number}</dd>
        <dt className="text-ink-secondary">Date</dt>
        <dd className="text-ink">{invoice.invoice_date}</dd>
        {invoice.device ? (
          <>
            <dt className="text-ink-secondary">Device</dt>
            <dd className="text-ink">
              {invoice.device.name ?? invoice.device.model_number}
              <span className="block tabular-nums text-ink-secondary">{invoice.device.serial_number}</span>
            </dd>
          </>
        ) : null}
      </dl>

      <ul aria-label="Line items" className="divide-y divide-hairline border-y border-hairline">
        {invoice.line_items.map((item, index) => (
          <li key={`${index}-${item.label}`} className="flex items-start justify-between gap-4 py-2.5 text-subheadline">
            <span className="min-w-0 break-words text-ink">{item.label}</span>
            <span className="shrink-0 tabular-nums text-ink">{item.amount_display}</span>
          </li>
        ))}
      </ul>

      <div className="flex items-end justify-between gap-4">
        <span className="pb-1.5 text-subheadline text-ink-secondary">Total</span>
        <span className="text-large-title font-semibold tabular-nums text-ink">{invoice.total_display}</span>
      </div>
    </Panel>
  );
}

// ---------- one screen per status ----------

function PaymentState({
  invoice,
  token,
  onSubmitted,
  onRefused,
}: {
  invoice: PayInvoice;
  token: string;
  onSubmitted: (utr: string, result: UtrResult) => void;
  onRefused: (err: PayError) => void;
}) {
  const form = (
    <UtrForm token={token} attemptsLeft={invoice.utr_attempts_left} onSubmitted={onSubmitted} onRefused={onRefused} />
  );

  switch (invoice.status) {
    case "pending":
      if (!invoice.upi) {
        return (
          <Notice icon={TriangleAlert} tint="text-warning" title="Online payment isn’t set up yet">
            <p>Please contact support: reply in the chat where you got this link, and we’ll help you pay.</p>
          </Notice>
        );
      }
      return (
        <>
          <PayWithUpi invoice={invoice} upi={invoice.upi} />
          <Panel label="Confirm your payment">
            <h2 className="text-title-2 font-semibold tracking-tight text-ink">Confirm your payment</h2>
            {form}
          </Panel>
        </>
      );

    case "verifying":
      return (
        <Panel label="Payment status">
          <div className="flex items-center gap-3">
            <LoaderCircle className="size-6 shrink-0 animate-spin text-accent" aria-hidden />
            <h2 className="text-title-2 font-semibold tracking-tight text-ink">Checking with the bank</h2>
          </div>
          <p className="text-subheadline text-ink-secondary">{invoice.message}</p>
          {invoice.utr ? (
            <p className="text-subheadline text-ink">
              UTR <span className="font-medium tabular-nums">{invoice.utr}</span>
            </p>
          ) : null}
          <CorrectUtr
            key={invoice.utr ?? ""}
            token={token}
            attemptsLeft={invoice.utr_attempts_left}
            onSubmitted={onSubmitted}
            onRefused={onRefused}
          />
        </Panel>
      );

    case "paid":
      return (
        <Panel label="Payment status" className="text-center">
          <CircleCheck className="mx-auto size-14 text-success" aria-hidden />
          <div className="space-y-1">
            <h2 className="text-title-1 font-semibold tracking-tight text-ink">Payment received</h2>
            <p className="text-subheadline text-ink-secondary">Thank you. A receipt has been sent to your email.</p>
          </div>
          <dl className="mx-auto grid w-fit grid-cols-[auto_auto] gap-x-4 gap-y-1 text-left text-subheadline">
            <dt className="text-ink-secondary">Amount</dt>
            <dd className="font-medium tabular-nums text-ink">{invoice.total_display}</dd>
            {invoice.utr ? (
              <>
                <dt className="text-ink-secondary">UTR</dt>
                <dd className="tabular-nums text-ink">{invoice.utr}</dd>
              </>
            ) : null}
            {invoice.paid_display ? (
              <>
                <dt className="text-ink-secondary">Paid on</dt>
                <dd className="tabular-nums text-ink">{invoice.paid_display}</dd>
              </>
            ) : null}
          </dl>
        </Panel>
      );

    case "failed":
      return (
        <Panel label="Payment status">
          <div className="flex items-start gap-3">
            <TriangleAlert className="mt-0.5 size-6 shrink-0 text-danger" aria-hidden />
            <div>
              <h2 className="text-title-2 font-semibold tracking-tight text-ink">We couldn’t match your payment</h2>
              {invoice.utr ? (
                <p className="text-footnote tabular-nums text-ink-secondary">UTR {invoice.utr}</p>
              ) : null}
            </div>
          </div>
          <p className="text-subheadline text-ink-secondary">{invoice.message}</p>
          {invoice.utr_attempts_left > 0 ? form : <p className="text-subheadline text-ink">{NO_ATTEMPTS_LEFT}</p>}
        </Panel>
      );

    case "expired":
      return (
        <Notice icon={Clock} tint="text-ink-secondary" title="This link has expired">
          <p>{invoice.message}</p>
          <p>Already paid? Send us your UTR in the chat where you got this link.</p>
        </Notice>
      );

    case "cancelled":
      return (
        <Notice icon={CircleX} tint="text-ink-secondary" title="This invoice was cancelled">
          <p>If you still need this service, ask us for a new link in the chat where you got this one.</p>
        </Notice>
      );

    default:
      return (
        <Notice icon={TriangleAlert} tint="text-ink-secondary" title={PILL[invoice.status]?.label ?? "Payment"}>
          <p>{invoice.message}</p>
        </Notice>
      );
  }
}

// ---------- paying ----------

function PayWithUpi({ invoice, upi }: { invoice: PayInvoice; upi: NonNullable<PayInvoice["upi"]> }) {
  return (
    <Panel label="Pay with UPI">
      <div className="space-y-1">
        <h2 className="text-title-2 font-semibold tracking-tight text-ink">Pay with UPI</h2>
        <p className="text-subheadline text-ink-secondary">Scan the code with any UPI app.</p>
      </div>

      {/* Always dark on white with a 4-module quiet zone, in dark mode too: phones can't scan it otherwise. */}
      <div className="mx-auto w-fit rounded-card bg-white p-2">
        <QRCodeSVG
          value={upi.uri}
          size={224}
          level="M"
          marginSize={4}
          bgColor="#FFFFFF"
          fgColor="#000000"
          title={`UPI QR code: pay ${invoice.total_display} to ${upi.payee_name}`}
        />
      </div>

      <a
        href={upi.uri}
        className="flex h-12 w-full items-center justify-center rounded-control bg-accent text-body font-semibold text-on-accent"
      >
        Pay with UPI app
      </a>

      <dl className="divide-y divide-hairline rounded-card bg-canvas dark:bg-surface-raised">
        <CopyRow label="UPI ID" shown={upi.upi_id} value={upi.upi_id} />
        <CopyRow label="Amount" shown={invoice.total_display} value={upi.amount} />
      </dl>

      <div className="space-y-2 text-footnote text-ink-secondary">
        <p>
          If your UPI app won’t open the link, scan the QR code from another phone, or pay{" "}
          <span className="break-all text-ink">{upi.upi_id}</span> exactly {invoice.total_display} from any UPI
          app.
        </p>
        <p>
          Your app should show <span className="text-ink">{upi.payee_name}</span> as the payee. This link expires on{" "}
          {invoice.expires_display}.
        </p>
      </div>
    </Panel>
  );
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    // No clipboard API outside a secure context (e.g. a phone on the LAN over plain http).
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try {
    copied = document.execCommand("copy");
  } catch {
    copied = false;
  }
  area.remove();
  return copied;
}

function CopyRow({ label, shown, value }: { label: string; shown: string; value: string }) {
  const [result, setResult] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);

  async function copy() {
    setResult((await copyText(value)) ? "copied" : "failed");
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setResult(null), 2000);
  }

  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2.5">
      <div className="min-w-0">
        <dt className="text-footnote text-ink-secondary">{label}</dt>
        <dd className="break-all text-subheadline font-medium tabular-nums text-ink">{shown}</dd>
      </div>
      <button
        type="button"
        onClick={() => void copy()}
        aria-label={`Copy ${label.toLowerCase()}`}
        className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-full bg-surface px-3 text-footnote font-medium text-accent outline-none focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface"
      >
        {result === "copied" ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
        <span aria-live="polite">{result === "copied" ? "Copied" : result === "failed" ? "Copy failed" : "Copy"}</span>
      </button>
    </div>
  );
}

// ---------- the UTR ----------

const NO_ATTEMPTS_LEFT =
  "No attempts are left on this link. Contact support: reply in the chat where you got this link, and we’ll check " +
  "your payment.";

/** While a UTR is checked: a typo can be fixed with the same form, counted against the same attempts. */
function CorrectUtr({
  token,
  attemptsLeft,
  onSubmitted,
  onRefused,
}: {
  token: string;
  attemptsLeft: number;
  onSubmitted: (utr: string, result: UtrResult) => void;
  onRefused: (err: PayError) => void;
}) {
  const [open, setOpen] = useState(false);

  if (attemptsLeft <= 0) {
    return (
      <p className="text-footnote text-ink-secondary">
        No attempts are left to change the UTR. If it’s wrong, contact support: reply in the chat where you got this
        link.
      </p>
    );
  }
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="rounded-control text-subheadline font-medium text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
      >
        Entered it wrong? Correct it
      </button>
    );
  }
  return (
    <UtrForm
      token={token}
      attemptsLeft={attemptsLeft}
      label="Enter the correct 12-digit UTR (UPI Ref No.) from your UPI app."
      submitLabel="Submit corrected UTR"
      onCancel={() => setOpen(false)}
      onSubmitted={(utr, result) => {
        setOpen(false);
        onSubmitted(utr, result);
      }}
      onRefused={onRefused}
    />
  );
}

function UtrForm({
  token,
  attemptsLeft,
  onSubmitted,
  onRefused,
  label = "After paying, enter the 12-digit UTR (UPI Ref No.) from your UPI app.",
  submitLabel = "Submit UTR",
  onCancel,
}: {
  token: string;
  attemptsLeft: number;
  onSubmitted: (utr: string, result: UtrResult) => void;
  onRefused: (err: PayError) => void;
  label?: string;
  submitLabel?: string;
  onCancel?: () => void;
}) {
  const [utr, setUtr] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const inputId = useId();
  const hintId = useId();
  const errorId = useId();

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (utr.length !== UTR_DIGITS) {
      setError(`A UTR has exactly ${UTR_DIGITS} digits. You’ve entered ${utr.length}.`);
      return;
    }
    setSending(true);
    setError(null);
    try {
      onSubmitted(utr, await pay.submitUtr(token, utr));
    } catch (err: unknown) {
      const refusal = err instanceof PayError ? err : new PayError(0, "Couldn’t send your UTR. Please try again.");
      setError(refusal.message);
      onRefused(refusal);
    } finally {
      setSending(false);
    }
  }

  if (attemptsLeft <= 0) {
    return <p className="text-subheadline text-ink">{NO_ATTEMPTS_LEFT}</p>;
  }

  return (
    <form onSubmit={(e) => void submit(e)} noValidate className="space-y-3">
      <label htmlFor={inputId} className="block text-subheadline text-ink">
        {label}
      </label>
      <input
        id={inputId}
        name="utr"
        value={utr}
        onChange={(e) => {
          // Digits only: a pasted "4275 1234 5678" or "UTR: 427512345678" keeps just the number.
          setUtr(e.target.value.replace(/\D/g, "").slice(0, 20));
          setError(null);
        }}
        inputMode="numeric"
        autoComplete="off"
        enterKeyHint="send"
        spellCheck={false}
        placeholder="12 digits"
        aria-describedby={error ? `${hintId} ${errorId}` : hintId}
        aria-invalid={error ? true : undefined}
        className="h-12 w-full rounded-control bg-canvas px-3 text-body tabular-nums tracking-wide text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent dark:bg-surface-raised"
      />
      <p id={hintId} className="text-footnote tabular-nums text-ink-secondary">
        {utr.length}/{UTR_DIGITS} digits · {attemptsLeft} attempt{attemptsLeft === 1 ? "" : "s"} left
      </p>
      {error ? (
        <p id={errorId} role="alert" className="text-footnote text-danger">
          {error}
        </p>
      ) : null}
      <div className="flex gap-2">
        {onCancel ? (
          <button
            type="button"
            onClick={onCancel}
            disabled={sending}
            className="h-12 shrink-0 rounded-control bg-canvas px-4 text-body font-medium text-ink outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-60 dark:bg-surface-raised"
          >
            Back
          </button>
        ) : null}
        <button
          type="submit"
          disabled={sending}
          className="flex h-12 w-full items-center justify-center rounded-control bg-accent text-body font-semibold text-on-accent disabled:opacity-60"
        >
          {sending ? "Sending…" : submitLabel}
        </button>
      </div>
    </form>
  );
}
