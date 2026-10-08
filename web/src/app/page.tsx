import type { LucideIcon } from "lucide-react";
import { ChevronRight, Headset, MessageCircle } from "lucide-react";
import Link from "next/link";

import { Brand } from "@/components/brand";
import { LaptopHero } from "@/components/home/laptop-hero";
import { ThemeToggle } from "@/components/theme-toggle";

// Home (§11.2): the company, an animated hero, and the two ways in.
export default function HomePage() {
  return (
    <div className="flex min-h-dvh flex-col">
      <header className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-4 sm:px-6">
        <Brand />
        <ThemeToggle />
      </header>

      <main className="mx-auto grid w-full max-w-6xl flex-1 content-center items-center gap-12 px-4 pt-6 pb-12 sm:px-6 lg:grid-cols-[minmax(0,2fr)_minmax(0,3fr)] lg:gap-12 lg:py-12 xl:gap-16">
        <div className="mx-auto w-full max-w-xl lg:max-w-none">
          <h1 className="text-large-title font-semibold text-balance">
            Help for your laptop, PC or headphones
          </h1>
          <p className="mt-3 text-pretty text-ink-secondary">
            Tell us what&apos;s wrong. We find your device by its serial number, open a ticket and
            reply right where you wrote to us.
          </p>

          <nav aria-label="Choose how to continue" className="mt-8">
            <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-1">
              <li>
                <RoleLink
                  href="/support"
                  icon={MessageCircle}
                  title="Go as customer"
                  description="Chat with support. No account needed."
                />
              </li>
              <li>
                <RoleLink
                  href="/login"
                  icon={Headset}
                  title="Login as service agent"
                  description="For agents, technicians and warehouse staff."
                />
              </li>
            </ul>
          </nav>
        </div>

        <LaptopHero className="mx-auto max-w-xl lg:max-w-none" />
      </main>

      <footer className="mx-auto w-full max-w-6xl px-4 pb-6 text-footnote text-ink-secondary sm:px-6">
        Service desk powered by ServiceMesh
      </footer>
    </div>
  );
}

function RoleLink({
  href,
  icon: Icon,
  title,
  description,
}: {
  href: string;
  icon: LucideIcon;
  title: string;
  description: string;
}) {
  return (
    <Link
      href={href}
      className="group flex h-full items-center gap-4 rounded-card bg-surface p-4 transition-colors hover:ring-1 hover:ring-hairline"
    >
      <span className="inline-flex size-10 shrink-0 items-center justify-center rounded-control bg-accent/10 text-accent">
        <Icon aria-hidden="true" className="size-5" />
      </span>
      <span className="min-w-0 flex-1">
        <span className="block font-semibold text-ink">{title}</span>
        <span className="block text-subheadline text-ink-secondary">{description}</span>
      </span>
      <ChevronRight
        aria-hidden="true"
        className="size-5 shrink-0 text-ink-secondary transition-transform group-hover:translate-x-0.5 group-hover:text-accent"
      />
    </Link>
  );
}
