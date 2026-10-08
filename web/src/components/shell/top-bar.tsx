"use client";

import { Bell, LogOut } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { DropdownMenu, Popover } from "radix-ui";

import { BrandMark, COMPANY_NAME } from "@/components/brand";
import { NAV } from "@/components/shell/nav";
import { useNotifications } from "@/components/shell/notifications";
import { EmptyState } from "@/components/states";
import { ThemeToggle } from "@/components/theme-toggle";
import { ROLE_LABEL, homeFor, signOut, type Staff } from "@/lib/auth";
import { initials, timeAgo } from "@/lib/format";
import { cn } from "@/lib/utils";

const FLOATING =
  "z-50 rounded-card bg-surface-raised shadow-raised outline-none data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0";

export function TopBar({ staff }: { staff: Staff }) {
  const pathname = usePathname();
  const items = NAV.filter((item) => item.roles.includes(staff.role));

  return (
    // §11.3: translucent, blurred, a hairline underneath.
    <header className="sticky top-0 z-40 border-b border-hairline bg-surface/75 backdrop-blur-[20px] backdrop-saturate-180">
      <div className="flex h-14 items-center gap-2 px-3 sm:gap-6 sm:px-5">
        <Link href={homeFor(staff.role)} className="flex shrink-0 items-center gap-2.5 rounded-control" aria-label={`${COMPANY_NAME} home`}>
          <BrandMark className="size-7" />
          <span className="hidden font-semibold tracking-tight text-ink md:inline">{COMPANY_NAME}</span>
        </Link>

        <nav aria-label="Main" className="min-w-0 flex-1">
          <ul className="flex items-center gap-1">
            {items.map((item) => {
              const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
              return (
                <li key={item.href}>
                  <Link
                    href={item.href}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "inline-flex h-9 items-center rounded-full px-3.5 text-subheadline transition-colors",
                      active ? "bg-hairline/50 font-semibold text-ink" : "text-ink-secondary hover:text-ink",
                    )}
                  >
                    {item.label}
                  </Link>
                </li>
              );
            })}
          </ul>
        </nav>

        <div className="flex shrink-0 items-center gap-1 sm:gap-2">
          <ThemeToggle className="hidden bg-transparent sm:inline-flex" />
          <NotificationBell />
          <AccountMenu staff={staff} />
        </div>
      </div>
    </header>
  );
}

function NotificationBell() {
  const { items, unread, markAllRead } = useNotifications();
  return (
    <Popover.Root onOpenChange={(open) => !open && markAllRead()}>
      <Popover.Trigger
        className="relative inline-flex size-9 items-center justify-center rounded-full text-ink-secondary transition-colors hover:bg-hairline/50 hover:text-ink data-[state=open]:bg-hairline/50 data-[state=open]:text-ink"
        aria-label={unread ? `Notifications, ${unread} new` : "Notifications"}
      >
        <Bell aria-hidden="true" className="size-5" />
        {unread > 0 && (
          // A dot, not a count: the label carries the number.
          <span aria-hidden="true" className="absolute top-2 right-2 size-2 rounded-full bg-danger ring-2 ring-surface" />
        )}
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content align="end" sideOffset={8} collisionPadding={12} className={cn(FLOATING, "w-80 max-w-[calc(100vw-1.5rem)]")}>
          <div className="flex items-center justify-between px-4 pt-3.5 pb-2">
            <p className="font-semibold">Notifications</p>
          </div>
          {items.length === 0 ? (
            <EmptyState icon={Bell} title="Nothing new" className="py-8">
              Notifications appear here as they arrive.
            </EmptyState>
          ) : (
            <ul className="max-h-96 overflow-y-auto px-1.5 pb-1.5">
              {items.map((n) => {
                const body = (
                  <>
                    <span className="flex items-start justify-between gap-3">
                      <span className="font-semibold text-ink">{n.title}</span>
                      <span className="shrink-0 text-footnote text-ink-secondary tabular-nums">{timeAgo(n.createdAt)}</span>
                    </span>
                    {n.body && <span className="mt-0.5 block text-subheadline text-ink-secondary">{n.body}</span>}
                  </>
                );
                const itemClass = cn("block rounded-control px-2.5 py-2 text-left", !n.read && "bg-accent/5");
                return (
                  <li key={n.id}>
                    {n.link?.startsWith("/") ? (
                      <Popover.Close asChild>
                        <Link href={n.link} className={cn(itemClass, "hover:bg-hairline/40")}>
                          {body}
                        </Link>
                      </Popover.Close>
                    ) : (
                      <div className={itemClass}>{body}</div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function AccountMenu({ staff }: { staff: Staff }) {
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger
        aria-label={`Account: ${staff.name}`}
        className="inline-flex size-9 items-center justify-center rounded-full bg-hairline/60 text-footnote font-semibold text-ink transition-colors hover:bg-hairline data-[state=open]:ring-2 data-[state=open]:ring-accent"
      >
        {initials(staff.name)}
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content align="end" sideOffset={8} collisionPadding={12} className={cn(FLOATING, "w-64 p-1.5")}>
          <div className="px-2.5 pt-2 pb-2.5">
            <p className="truncate font-semibold">{staff.name}</p>
            <p className="truncate text-subheadline text-ink-secondary">{staff.email}</p>
            <p className="mt-1 text-footnote text-ink-secondary">{ROLE_LABEL[staff.role]}</p>
          </div>
          <div className="flex items-center justify-between gap-3 border-t border-hairline px-2.5 py-2.5 sm:hidden">
            <span className="text-subheadline text-ink-secondary">Appearance</span>
            <ThemeToggle className="bg-canvas" />
          </div>
          <DropdownMenu.Separator className="mx-1 my-1 h-px bg-hairline" />
          <DropdownMenu.Item
            onSelect={signOut} // the shell then goes to /login
            className="flex cursor-default items-center gap-2.5 rounded-control px-2.5 py-2 text-ink outline-none select-none data-[highlighted]:bg-hairline/50"
          >
            <LogOut aria-hidden="true" className="size-4 text-ink-secondary" />
            Sign out
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}
