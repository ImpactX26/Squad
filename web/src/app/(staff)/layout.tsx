"use client";

import { usePathname, useRouter } from "next/navigation";
import { type ReactNode, useEffect, useState } from "react";

import { AnyQuestionsSidebar } from "@/components/any-questions/sidebar";
import { ErrorState, LoadingState } from "@/components/states";
import { TopBar } from "@/components/top-bar";
import { AnyQuestionsProvider } from "@/lib/any-questions";
import { ApiError, type StaffUser, api, clearToken, getToken } from "@/lib/api";
import { StaffUserContext } from "@/lib/session";

type Session = { status: "loading" } | { status: "ready"; user: StaffUser } | { status: "error"; message: string };

/**
 * Staff app shell: requires a valid session, then renders the top bar above the page, with the
 * "Any Questions" sidebar beside it for agents and admins (§11.2).
 */
export default function StaffLayout({ children }: { children: ReactNode }) {
  const router = useRouter();
  // The inbox's card grid uses the full width; the other pages keep a readable measure.
  const wide = usePathname() === "/inbox";
  const [session, setSession] = useState<Session>({ status: "loading" });

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api
      .me()
      .then((user) => setSession({ status: "ready", user }))
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 401) {
          clearToken();
          router.replace("/login");
        } else {
          setSession({ status: "error", message: err instanceof Error ? err.message : "Something went wrong." });
        }
      });
  }, [router]);

  if (session.status === "loading") {
    return (
      <main className="mx-auto min-h-dvh max-w-7xl bg-canvas px-4 py-6">
        <LoadingState label="Signing in" rows={4} />
      </main>
    );
  }

  if (session.status === "error") {
    return (
      <main className="grid min-h-dvh place-items-center bg-canvas px-4">
        <div className="w-full max-w-md">
          <ErrorState message={session.message} onRetry={() => window.location.reload()} />
        </div>
      </main>
    );
  }

  const { role } = session.user;
  return (
    <StaffUserContext value={session.user}>
      <AnyQuestionsProvider available={role === "agent" || role === "admin"}>
        <div className="min-h-dvh bg-canvas">
          <TopBar user={session.user} />
          <div className="flex">
            <main className="min-w-0 flex-1 px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
              <div className={`mx-auto ${wide ? "max-w-[1680px]" : "max-w-7xl"}`}>{children}</div>
            </main>
            <AnyQuestionsSidebar />
          </div>
        </div>
      </AnyQuestionsProvider>
    </StaffUserContext>
  );
}
