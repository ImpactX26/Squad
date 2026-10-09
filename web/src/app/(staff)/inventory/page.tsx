"use client";

// The inventory page (ARCHITECTURE.md §7.8, §11.2): stock by warehouse with what is running low,
// the restock requests and their alert, the parts tickets hold, and what was used this week.
// GET /api/inventory and /api/restock-requests (warehouse staff and admins); refetched live when
// /ws/staff reports a booking, a completed job or stock.low (§9).
//
// Read-only. Stock moves only through the workflows (§4.2), and §10 has no route to mark a restock
// request ordered or received, so the page says so instead of offering a button that can't work.

import { AlertTriangle, Boxes, PackageCheck, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { EmptyState, ErrorState, InlineError, LoadingState, errorMessage } from "@/components/states";
import { LiveBadge } from "@/components/ticket/live-badge";
import { ApiError, api } from "@/lib/api";
import { useStaffUser } from "@/lib/session";
import type { components } from "@/lib/api-types";
import { absolute, relative } from "@/lib/format";
import { type SocketStatus, type StaffEvent, openStaffSocket } from "@/lib/ws";

type Inventory = components["schemas"]["InventoryResponse"];
type Restock = components["schemas"]["RestockRequestOut"];
type Load =
  | { state: "loading" }
  | { state: "error"; message: string; forbidden: boolean }
  | { state: "ready"; inventory: Inventory; restock: Restock[] };
type LowAlert = { sku: string; name: string; available: number; qty: number; at: string };

// Events after which stock or reservations may have moved (§9).
const STOCK_EVENTS = new Set<StaffEvent["type"]>([
  "stock.low",
  "job.assigned",
  "job.status_changed",
  "job.completed",
  "payment.paid",
]);

const REQUEST_STATUS: Record<string, string> = {
  open: "Open",
  ordered: "Ordered",
  received: "Received",
  cancelled: "Cancelled",
};
const JOB_STATUS: Record<string, string> = {
  assigned: "Assigned",
  accepted: "Accepted",
  en_route: "On the way",
  on_site: "On site",
  completed: "Completed",
  cancelled: "Cancelled",
};

const TH = "px-3 py-2 text-left text-footnote font-medium text-ink-secondary";
const TD = "px-3 py-2.5 text-subheadline text-ink";
const NUM = `${TD} text-right tabular-nums`;

export default function InventoryPage() {
  const role = useStaffUser()?.role;
  // §11.2: warehouse staff and admins. Anyone else is told so, without calls the API would refuse.
  const allowed = role === "warehouse" || role === "admin";
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [socket, setSocket] = useState<SocketStatus>("connecting");
  const [revision, setRevision] = useState(0);
  const [lowAlerts, setLowAlerts] = useState<LowAlert[]>([]);
  const [filter, setFilter] = useState("");
  const [lowOnly, setLowOnly] = useState(false);

  useEffect(() => {
    if (!allowed) return;
    let cancelled = false;
    void (async () => {
      try {
        const [inventory, restock] = await Promise.all([api.inventory(), api.restockRequests()]);
        if (cancelled) return;
        setLoad({ state: "ready", inventory, restock: restock.requests });
        setRefreshError(null);
      } catch (err: unknown) {
        if (cancelled) return;
        const message = errorMessage(err, "Couldn’t load the inventory.");
        const forbidden = err instanceof ApiError && err.status === 403;
        setLoad((current) => (current.state === "ready" ? current : { state: "error", message, forbidden }));
        setRefreshError(message);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [revision, allowed]);

  useEffect(() => {
    if (!allowed) return;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const close = openStaffSocket((event) => {
      if (!STOCK_EVENTS.has(event.type)) return;
      if (event.type === "stock.low") {
        const d = event.data;
        setLowAlerts((current) => [
          { sku: String(d.sku ?? ""), name: String(d.name ?? ""), available: Number(d.available ?? 0),
            qty: Number(d.qty ?? 0), at: event.ts },
          ...current,
        ].slice(0, 5));
      }
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => setRevision((n) => n + 1), 300);
    }, setSocket);
    return () => {
      if (timer) clearTimeout(timer);
      close();
    };
  }, [allowed]);

  const retry = () => setRevision((n) => n + 1);

  if (!allowed) return <ErrorState message="Inventory is for warehouse staff and admins." />;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
        <h1 className="text-title-1 font-semibold tracking-tight text-ink">Inventory</h1>
        {load.state === "ready" ? <Summary inventory={load.inventory} restock={load.restock} /> : null}
        <LiveBadge status={socket} className="ml-auto" />
      </header>

      {lowAlerts.map((alert) => (
        <div key={`${alert.sku}-${alert.at}`} role="alert"
             className="flex items-start gap-3 rounded-card bg-warning/15 px-4 py-3 text-subheadline text-ink animate-in fade-in">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <p>
            <span className="font-medium">Low stock just now: {alert.sku}</span> {alert.name}, {alert.available} available.
            A restock request for {alert.qty} was opened; the completion workflow emails the warehouse alert address
            and notifies admins (§7.8).
          </p>
        </div>
      ))}

      {load.state === "loading" ? <LoadingState label="Loading inventory" rows={5} rowClassName="h-12" /> : null}
      {load.state === "error" ? (
        <ErrorState message={load.forbidden ? "Inventory is for warehouse staff and admins." : load.message}
                    onRetry={load.forbidden ? undefined : retry} />
      ) : null}

      {load.state === "ready" ? (
        <>
          {refreshError ? <InlineError message={refreshError} onRetry={retry} /> : null}
          <RestockSection requests={load.restock} />
          <StockSection items={load.inventory.items} filter={filter} onFilter={setFilter}
                        lowOnly={lowOnly} onLowOnly={setLowOnly} />
          <div className="grid gap-6 lg:grid-cols-2">
            <ReservationsSection reservations={load.inventory.reservations} />
            <UsageSection inventory={load.inventory} />
          </div>
        </>
      ) : null}
    </div>
  );
}

function Summary({ inventory, restock }: { inventory: Inventory; restock: Restock[] }) {
  const low = inventory.items.filter((i) => i.low).length;
  const reserved = inventory.items.reduce((sum, i) => sum + i.reserved, 0);
  const open = restock.filter((r) => r.status === "open" || r.status === "ordered").length;
  const used = inventory.usage.reduce((sum, u) => sum + u.used, 0);
  return (
    <p className="text-subheadline tabular-nums text-ink-secondary">
      {inventory.items.length} stock lines · <span className={low ? "font-medium text-warning" : ""}>{low} running low</span>
      {" · "}{reserved} reserved · {open} restock request{open === 1 ? "" : "s"} open · {used} used this week
    </p>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="space-y-2">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <h2 className="text-title-2 font-semibold tracking-tight text-ink">{title}</h2>
        {hint ? <p className="text-footnote text-ink-secondary">{hint}</p> : null}
      </div>
      {children}
    </section>
  );
}

function RestockSection({ requests }: { requests: Restock[] }) {
  const open = requests.filter((r) => r.status === "open" || r.status === "ordered");
  return (
    <Section
      title="Restock requests"
      hint="Opened automatically when a booking or a completed job takes a part to its threshold (once per drop), with an email to the warehouse and a note to admins. Marking one ordered or received isn’t built: §10 has no route for it."
    >
      {open.length ? (
        <div role="alert" className="flex items-start gap-3 rounded-card bg-warning/15 px-4 py-3 text-subheadline text-ink">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
          <p>
            {open.length} part{open.length === 1 ? " needs" : "s need"} restocking:{" "}
            {open.map((r) => `${r.sku} (${r.qty})`).join(", ")}.
          </p>
        </div>
      ) : null}
      {requests.length === 0 ? (
        <EmptyState icon={PackageCheck} title="No restock requests."
                    hint="When a completed repair takes a part to its reorder threshold, a request appears here and the warehouse is emailed." />
      ) : (
        <div className="overflow-x-auto rounded-card bg-surface">
          <table className="w-full min-w-[40rem]">
            <thead className="border-b border-hairline">
              <tr>
                <th className={TH}>Part</th>
                <th className={TH}>Warehouse</th>
                <th className={`${TH} text-right`}>Qty</th>
                <th className={`${TH} text-right`}>Available now</th>
                <th className={TH}>Status</th>
                <th className={TH}>Opened</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {requests.map((r) => (
                <tr key={r.id}>
                  <td className={TD}>
                    <span className="font-medium tabular-nums">{r.sku}</span>{" "}
                    <span className="text-ink-secondary">{r.part_name}</span>
                    {r.reason ? <p className="text-footnote text-ink-secondary">{r.reason}</p> : null}
                  </td>
                  <td className={TD}>{r.warehouse_name}</td>
                  <td className={NUM}>{r.qty}</td>
                  <td className={NUM}>{r.available_now ?? "—"}</td>
                  <td className={TD}>
                    <Pill tone={r.status === "open" ? "warning" : r.status === "ordered" ? "accent" : "muted"}>
                      {REQUEST_STATUS[r.status] ?? r.status}
                    </Pill>
                  </td>
                  <td className={`${TD} text-ink-secondary`} title={absolute(r.created_at)}>{relative(r.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function StockSection({
  items,
  filter,
  onFilter,
  lowOnly,
  onLowOnly,
}: {
  items: Inventory["items"];
  filter: string;
  onFilter: (value: string) => void;
  lowOnly: boolean;
  onLowOnly: (value: boolean) => void;
}) {
  const shown = useMemo(() => {
    const words = filter.trim().toLowerCase();
    return items.filter(
      (i) =>
        (!lowOnly || i.low) &&
        (!words || `${i.sku} ${i.name} ${i.part_type} ${i.warehouse_name}`.toLowerCase().includes(words)),
    );
  }, [items, filter, lowOnly]);

  return (
    <Section title="Stock by warehouse" hint="Available is on hand minus what booked repairs have reserved. Running low first.">
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={filter}
          onChange={(e) => onFilter(e.target.value)}
          placeholder="Filter by SKU, part or type"
          aria-label="Filter stock"
          className="h-9 min-w-0 flex-1 rounded-control bg-surface px-3 text-subheadline text-ink outline-none placeholder:text-ink-secondary focus-visible:ring-2 focus-visible:ring-accent sm:max-w-xs"
        />
        <label className="inline-flex items-center gap-2 text-subheadline text-ink-secondary">
          <input type="checkbox" checked={lowOnly} onChange={(e) => onLowOnly(e.target.checked)} className="accent-accent" />
          Running low only
        </label>
      </div>
      {items.length === 0 ? (
        <EmptyState icon={Boxes} title="No stock recorded." hint="Parts appear here once they are stocked in a warehouse (the seed stocks every demo part)." />
      ) : shown.length === 0 ? (
        <EmptyState icon={Boxes} title={lowOnly ? "Nothing is running low." : "No part matches that filter."} />
      ) : (
        <div className="overflow-x-auto rounded-card bg-surface">
          <table className="w-full min-w-[44rem]">
            <thead className="border-b border-hairline">
              <tr>
                <th className={TH}>Part</th>
                <th className={TH}>Type</th>
                <th className={TH}>Warehouse</th>
                <th className={`${TH} text-right`}>On hand</th>
                <th className={`${TH} text-right`}>Reserved</th>
                <th className={`${TH} text-right`}>Available</th>
                <th className={`${TH} text-right`}>Reorder at</th>
                <th className={TH}>State</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-hairline">
              {shown.map((i) => (
                <tr key={`${i.part_id}-${i.warehouse_id}`} className={i.low ? "bg-warning/6" : undefined}>
                  <td className={TD}>
                    <span className="font-medium tabular-nums">{i.sku}</span>{" "}
                    <span className="text-ink-secondary">{i.name}</span>
                  </td>
                  <td className={`${TD} text-ink-secondary`}>{i.part_type.replace(/_/g, " ")}</td>
                  <td className={`${TD} text-ink-secondary`}>{i.warehouse_name}</td>
                  <td className={NUM}>{i.on_hand}</td>
                  <td className={NUM}>{i.reserved}</td>
                  <td className={`${NUM} font-medium`}>{i.available}</td>
                  <td className={`${NUM} text-ink-secondary`}>{i.reorder_threshold}</td>
                  <td className={TD}>
                    {i.restock ? (
                      <Pill tone="accent">Restock {REQUEST_STATUS[i.restock.status]?.toLowerCase() ?? i.restock.status} ({i.restock.qty})</Pill>
                    ) : i.low ? (
                      <Pill tone="warning">Running low</Pill>
                    ) : (
                      <Pill tone="success">In stock</Pill>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Section>
  );
}

function ReservationsSection({ reservations }: { reservations: Inventory["reservations"] }) {
  return (
    <Section title="Reserved for repairs" hint="Parts booked repairs hold until the job is completed or cancelled.">
      {reservations.length === 0 ? (
        <EmptyState icon={Boxes} title="No part is reserved." hint="A part is reserved when a paid or warranty repair is booked." />
      ) : (
        <ul className="divide-y divide-hairline overflow-hidden rounded-card bg-surface">
          {reservations.map((r) => (
            <li key={`${r.ticket_id}-${r.sku}`} className="flex items-start gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-subheadline text-ink">
                  <span className="font-medium tabular-nums">{r.qty} × {r.sku}</span>{" "}
                  <span className="text-ink-secondary">{r.part_name}</span>
                </p>
                <p className="text-footnote text-ink-secondary">
                  <Link href={`/tickets/${r.ticket_id}`} prefetch={false} className="text-accent hover:underline">
                    {r.ticket_number}
                  </Link>
                  {" · "}{r.warehouse_name} · since {relative(r.since)}
                </p>
              </div>
              <Pill tone="muted">{r.job_status ? `Job ${JOB_STATUS[r.job_status]?.toLowerCase() ?? r.job_status}` : "No job yet"}</Pill>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function UsageSection({ inventory }: { inventory: Inventory }) {
  const most = Math.max(1, ...inventory.usage.map((u) => u.used));
  return (
    <Section title="Used this week" hint={`Parts consumed by completed jobs in the last ${inventory.usage_days} days.`}>
      {inventory.usage.length === 0 ? (
        <EmptyState icon={PackageCheck} title="No parts used this week." hint="Each completed repair consumes its reserved part." />
      ) : (
        <ul className="space-y-2 rounded-card bg-surface p-4">
          {inventory.usage.map((u) => (
            <li key={u.part_type} className="grid grid-cols-[7rem_minmax(0,1fr)_2.5rem] items-center gap-3 text-subheadline">
              <span className="text-ink">{u.part_type.replace(/_/g, " ")}</span>
              <span className="h-2 rounded-full bg-canvas dark:bg-surface-raised">
                <span className="block h-2 rounded-full bg-accent" style={{ width: `${(u.used / most) * 100}%` }} />
              </span>
              <span className="text-right tabular-nums text-ink">{u.used}</span>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

function Pill({ tone, children }: { tone: "warning" | "accent" | "success" | "muted"; children: React.ReactNode }) {
  const tones = {
    warning: "bg-warning/15 text-warning",
    accent: "bg-accent/12 text-accent",
    success: "bg-success/15 text-success",
    muted: "bg-canvas text-ink-secondary dark:bg-surface-raised",
  };
  return (
    <span className={`inline-flex whitespace-nowrap rounded-full px-2.5 py-0.5 text-footnote font-medium ${tones[tone]}`}>
      {children}
    </span>
  );
}
