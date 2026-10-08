"use client";

import { ShieldAlert } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { rolesFor } from "@/components/shell/nav";
import { NotificationsProvider } from "@/components/shell/notifications";
import { StaffEventsProvider } from "@/components/shell/staff-events";
import { TopBar } from "@/components/shell/top-bar";
import { EmptyState, LoadingState } from "@/components/states";
import { buttonVariants } from "@/components/ui/button";
import { ROLE_LABEL, homeFor, takeSignOut, useSession } from "@/lib/auth";

/**
 * Every staff page sits in this shell. The token lives in the browser (lib/auth.ts), so the
 * guard is here: no session → /login?next=<this page>; the wrong role → a plain explanation.
 */
export function StaffShell({ children }: { children: ReactNode }) {
  const session = useSession();
  const router = useRouter();
  const pathname = usePathname();

  useEffect(() => {
    if (session === null) {
      const here = window.location.pathname + window.location.search;
      router.replace(takeSignOut() ? "/login" : `/login?next=${encodeURIComponent(here)}`);
    }
  }, [session, router]);

  if (!session) return <LoadingState className="min-h-dvh" label={session === null ? "Signing in" : "Loading"} />;

  const allowed = rolesFor(pathname);
  const refused = allowed !== null && !allowed.includes(session.staff.role);

  return (
    <NotificationsProvider>
      <StaffEventsProvider token={session.access_token} staffId={session.staff.id}>
      <div className="flex min-h-dvh flex-col">
        <TopBar staff={session.staff} />
        {refused ? (
          <EmptyState
            icon={ShieldAlert}
            title="This page isn't for your role"
            className="flex-1"
            action={
              <Link href={homeFor(session.staff.role)} className={buttonVariants({ variant: "secondary", size: "sm" })}>
                Go to your page
              </Link>
            }
          >
            You&apos;re signed in as {ROLE_LABEL[session.staff.role].toLowerCase()}.
          </EmptyState>
        ) : (
          children
        )}
      </div>
      </StaffEventsProvider>
    </NotificationsProvider>
  );
}
