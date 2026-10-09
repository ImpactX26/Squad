import Link from "next/link";
import type { ReactNode } from "react";

import { ActivityRail, type ToolCall } from "@/components/activity-rail/activity-rail";
import { DiagnosticsPanel } from "@/components/ticket/diagnostics-panel";
import { MarkPaid } from "@/components/ticket/mark-paid";
import type { DiagnosticStep, SourceChannel, TicketDetail, TicketPayment } from "@/lib/api";
import { CHANNEL_LABEL, absolute, dateOnly } from "@/lib/format";
import { JOB_STATUS_LABEL } from "@/lib/jobs";
import { useStaffUser } from "@/lib/session";

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="rounded-card bg-surface p-4">
      <h2 className="mb-2 text-subheadline font-semibold text-ink">{title}</h2>
      {children}
    </section>
  );
}

/** A real empty state: says what is missing and why, and shows nothing invented. */
function Empty({ children }: { children: ReactNode }) {
  return <p className="text-footnote text-ink-secondary">{children}</p>;
}

function Line({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  return <p className={`text-subheadline ${muted ? "text-ink-secondary" : "text-ink"}`}>{children}</p>;
}

function money(amount: number, currency: string): string {
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount);
  } catch {
    return `${amount} ${currency}`;
  }
}

function sentence(value: string): string {
  const text = value.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Section 7.6: an admin may confirm a payment by hand once it is waiting on, or failed, its bank alert. */
function awaitsReview(payment: TicketPayment): boolean {
  return payment.status === "verifying" || payment.status === "failed" || payment.needs_review;
}

function warranty(until: string | null): { text: string; out: boolean } | null {
  if (!until) return null;
  const out = new Date(`${until}T23:59:59`).getTime() < Date.now();
  return { text: out ? `Out of warranty since ${dateOnly(until)}` : `In warranty until ${dateOnly(until)}`, out };
}

export function RightRail({
  ticket,
  toolCalls,
  channels,
  onStepChange,
  onPaymentChange,
}: {
  ticket: TicketDetail;
  toolCalls: ToolCall[];
  channels: SourceChannel[];
  onStepChange: (step: DiagnosticStep) => void;
  onPaymentChange: () => void;
}) {
  const { customer, product, payment, job } = ticket;
  const cover = warranty(product?.warranty_until ?? null);
  const isAdmin = useStaffUser()?.role === "admin";

  return (
    <div className="space-y-3">
      <Card title="Customer">
        {customer ? (
          <>
            <Line>{customer.full_name ?? "Unnamed customer"}</Line>
            {customer.email ? <Line muted>{customer.email}</Line> : null}
            <Line muted>{channels.map((c) => CHANNEL_LABEL[c]).join(", ")}</Line>
          </>
        ) : (
          <Empty>No customer is linked to this ticket.</Empty>
        )}
      </Card>

      <Card title="Device">
        {product ? (
          <>
            <Line>{[product.model_name, product.color].filter(Boolean).join(", ")}</Line>
            <Line muted>
              <span className="tabular-nums">{product.serial_number}</span>
            </Line>
            {cover ? (
              <p className={`text-subheadline ${cover.out ? "text-warning" : "text-ink-secondary"}`}>{cover.text}</p>
            ) : null}
          </>
        ) : (
          <Empty>No device has been verified yet. Intake asks the customer for a serial number.</Empty>
        )}
      </Card>

      <Card title="Diagnostics">
        <DiagnosticsPanel ticketId={ticket.id} steps={ticket.diagnostic_steps} onStepChange={onStepChange} />
      </Card>

      <Card title="Payment">
        {payment ? (
          <>
            <Line>
              <span className="tabular-nums">{money(payment.amount, payment.currency)}</span>, {payment.service_code}
            </Line>
            {payment.invoice_number ? (
              <Line muted>
                <span className="tabular-nums">{payment.invoice_number}</span>
              </Line>
            ) : null}
            <Line muted>
              {sentence(payment.status)}
              {payment.paid_at ? (
                <> on <time dateTime={payment.paid_at}>{absolute(payment.paid_at)}</time></>
              ) : payment.status === "pending" ? (
                <> until <time dateTime={payment.expires_at}>{absolute(payment.expires_at)}</time></>
              ) : null}
            </Line>
            {payment.utr ? (
              <Line muted>
                UTR <span className="tabular-nums">{payment.utr}</span>
              </Line>
            ) : null}
            {payment.needs_review ? (
              <p className="text-subheadline text-warning">
                Needs review: the bank alert didn’t confirm this payment. Check the bank statement.
              </p>
            ) : null}
            {isAdmin && awaitsReview(payment) ? <MarkPaid paymentId={payment.id} onPaid={onPaymentChange} /> : null}
          </>
        ) : (
          <Empty>No payment on this ticket. Payment links arrive with the /payments command.</Empty>
        )}
      </Card>

      <Card title="Job">
        {job ? (
          <>
            <Line>{job.technician_name ?? "Technician not named"}</Line>
            <Line muted>
              <span className="tabular-nums">{dateOnly(job.scheduled_date)}</span>
              {" · "}
              {JOB_STATUS_LABEL[job.status]}
            </Line>
            <Line muted>
              {job.part ? (
                <>
                  <span className="tabular-nums">{job.part.sku}</span>
                  {job.part.warehouse_name ? `, reserved at ${job.part.warehouse_name}` : null}
                </>
              ) : (
                "No part needed"
              )}
            </Line>
            <Line muted>{job.billing === "paid" ? `Paid, ${job.invoice_number ?? "invoice on file"}` : "Free under warranty"}</Line>
            <Link
              href={`/jobs/${job.id}`}
              className="mt-1 inline-block rounded-control text-subheadline text-accent outline-none hover:underline focus-visible:ring-2 focus-visible:ring-accent"
            >
              Open the job
            </Link>
          </>
        ) : (
          <Empty>No technician visit is booked. One is booked when a payment is confirmed, or a warranty repair’s details are in.</Empty>
        )}
      </Card>

      <Card title="Agent activity">
        <ActivityRail calls={toolCalls} />
      </Card>
    </div>
  );
}
