"use client";

import { LogOut, MessageCircleQuestion } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { LogoMark } from "@/components/brand";
import { SearchPalette } from "@/components/search/search-palette";
import { ThemeToggle } from "@/components/theme-toggle";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useAnyQuestions } from "@/lib/any-questions";
import { HOME_BY_ROLE, type StaffRole, type StaffUser, clearToken } from "@/lib/api";

// §11.2 "Who" column decides which roles see each page.
// `built: false` marks pages that don't exist yet (section 14.2, Phase 4). They show as inert text,
// because a link to a missing route is a 404 for the user and a console error for every prefetch.
const NAV: { href: string; label: string; roles: StaffRole[]; built: boolean }[] = [
  { href: "/inbox", label: "Inbox", roles: ["agent", "admin"], built: true },
  { href: "/jobs", label: "Jobs", roles: ["technician"], built: true },
  { href: "/inventory", label: "Inventory", roles: ["warehouse", "admin"], built: true },
  { href: "/payments", label: "Payments", roles: ["agent", "admin"], built: true },
  { href: "/commands", label: "Commands", roles: ["agent", "admin"], built: true },
];

const ROLE_LABEL: Record<StaffRole, string> = {
  agent: "Agent",
  technician: "Technician",
  warehouse: "Warehouse",
  admin: "Admin",
};

function initials(name: string): string {
  return name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? "")
    .join("");
}

export function TopBar({ user }: { user: StaffUser }) {
  const pathname = usePathname();
  const router = useRouter();
  const questions = useAnyQuestions();

  function signOut() {
    clearToken();
    router.replace("/login");
  }

  return (
    // A floating bar (§11.3): 56px tall in all, so the Any Questions sidebar's top-14 still lines up under it.
    <header className="sticky top-0 z-50 h-14 px-2 pt-1.5 sm:px-4">
      <div className="flex h-12 items-center gap-3 rounded-[20px] bg-frost pr-2 pl-2.5 shadow-[0_14px_34px_-22px_color-mix(in_oklab,var(--accent)_55%,transparent)] ring-1 ring-white/70 backdrop-blur-[20px] backdrop-saturate-[1.6] sm:gap-5 dark:ring-white/8">
        <Link
          href={HOME_BY_ROLE[user.role]}
          className="flex shrink-0 items-center gap-2 rounded-control outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          <LogoMark className="size-8" />
          <span className="hidden text-[19px] font-bold tracking-[-0.03em] sm:inline">ServiceMesh</span>
        </Link>

        {/* On a phone the links scroll sideways inside the bar instead of widening the page. */}
        <nav aria-label="Main" className="flex min-w-0 items-center gap-1 overflow-x-auto rounded-full bg-canvas/80 p-1 whitespace-nowrap ring-1 ring-hairline/70 [scrollbar-width:none] dark:bg-surface-raised/60">
          {NAV.filter((item) => item.roles.includes(user.role)).map((item) => {
            if (!item.built) {
              return (
                <span key={item.href} className="text-subheadline text-ink-secondary/60" title="Not built yet">
                  {item.label}
                </span>
              );
            }
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-full px-4 py-1.5 text-[14px] tracking-[0.02em] outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent ${
                  active
                    ? "bg-accent font-semibold text-on-accent shadow-[0_6px_14px_-8px_color-mix(in_oklab,var(--accent)_80%,transparent)]"
                    : "font-semibold text-ink-secondary hover:bg-surface hover:text-ink"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        <div className="ml-auto flex shrink-0 items-center gap-2">
          {user.role === "agent" || user.role === "admin" ? <SearchPalette /> : null}
          {questions.available ? (
            <button
              type="button"
              onClick={questions.toggle}
              aria-expanded={questions.open}
              aria-controls="any-questions"
              aria-keyshortcuts="Control+Period Meta+Period"
              title={`${questions.open ? "Close" : "Open"} Any Questions (${questions.shortcut})`}
              className={`inline-flex h-9 items-center gap-2 rounded-full px-3 text-[15px] md:pr-1.5 font-semibold outline-none transition active:scale-[0.97] focus-visible:ring-2 focus-visible:ring-violet focus-visible:ring-offset-2 focus-visible:ring-offset-surface ${
                questions.open
                  ? "bg-violet/14 text-violet hover:bg-violet/20"
                  : "bg-surface text-violet shadow-card hover:-translate-y-px"
              }`}
            >
              <MessageCircleQuestion className="size-4" aria-hidden />
              <span className="hidden md:inline">Any Questions</span>
              <kbd
                className={`hidden rounded-full px-2 py-0.5 font-sans text-[12px] font-medium md:inline ${
                  questions.open ? "bg-violet/14" : "bg-violet/10"
                }`}
              >
                {questions.shortcut}
              </kbd>
            </button>
          ) : null}
          <ThemeToggle />
          <DropdownMenu>
            <DropdownMenuTrigger
              aria-label={`Account: ${user.name}`}
              className="grid size-8 place-items-center rounded-full bg-accent text-footnote font-semibold text-on-accent outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
            >
              {initials(user.name)}
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-60">
              <DropdownMenuLabel className="font-normal">
                <div className="text-subheadline font-medium text-ink">{user.name}</div>
                <div className="truncate text-footnote text-ink-secondary">{user.email}</div>
                <div className="text-footnote text-ink-secondary">{ROLE_LABEL[user.role]}</div>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem onSelect={signOut}>
                <LogOut />
                Sign out
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>
    </header>
  );
}
