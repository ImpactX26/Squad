import type { Metadata } from "next";

import { BrandMark, COMPANY_NAME } from "@/components/brand";
import { HomeLink } from "@/components/home-link";
import { LoginForm } from "@/components/login-form";
import { ThemeToggle } from "@/components/theme-toggle";

export const metadata: Metadata = { title: "Sign in" };

// Staff sign-in (§11.2). No sign-up: customers never need an account.
export default function LoginPage() {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="mx-auto flex h-16 w-full max-w-5xl items-center justify-between px-4 sm:px-6">
        <HomeLink className="-ml-2" />
        <ThemeToggle />
      </header>

      <main className="mx-auto grid w-full max-w-5xl flex-1 content-center items-center gap-10 px-4 pb-16 sm:px-6 md:grid-cols-2 md:gap-16">
        <section aria-label={COMPANY_NAME} className="flex flex-col items-center text-center md:items-start md:text-left">
          <BrandMark className="size-16 md:size-20" />
          <h1 className="mt-5 text-title-1 font-semibold md:text-large-title">{COMPANY_NAME}</h1>
          <p className="mt-2 max-w-sm text-pretty text-ink-secondary">
            The service desk for agents, technicians and warehouse staff.
          </p>
        </section>

        <section className="w-full max-w-md justify-self-center rounded-panel bg-surface p-6 sm:p-8 md:justify-self-end">
          <h2 className="text-title-2 font-semibold">Sign in</h2>
          <p className="mt-1 text-subheadline text-ink-secondary">Use your work email.</p>
          <LoginForm />
        </section>
      </main>
    </div>
  );
}
