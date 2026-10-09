"use client";

// The welcome screen (ARCHITECTURE.md §11.2): who we are, and the two ways in, as one Customer | Agent
// switch. Home (/) opens on Customer, /login on Agent.
//
// Customers never need an account: their way in is the support chat (/support). Staff sign in with their
// work email and password (POST /api/auth/login); there is no other sign-in method and no sign-up.

import { Eye, EyeOff, Headphones, LoaderCircle, MessageCircle, UserRound } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { type FormEvent, type KeyboardEvent, useId, useRef, useState } from "react";

import { LogoMark } from "@/components/brand";
import { ChannelGlyph } from "@/components/ticket/glyphs";
import { Perforation, StubPriority, StubTop, stubColours } from "@/components/ticket/ticket-stub";
import { ThemeToggle } from "@/components/theme-toggle";
import { ApiError, HOME_BY_ROLE, api, setToken } from "@/lib/api";

type Mode = "customer" | "agent";

const FIELD =
  "h-12 w-full rounded-control border border-hairline bg-surface px-4 text-[15px] text-ink outline-none transition-shadow " +
  "placeholder:text-ink-secondary/70 focus-visible:border-violet focus-visible:shadow-[0_0_0_4px_color-mix(in_oklab,var(--violet)_18%,transparent)]";

export function Welcome({ initial }: { initial: Mode }) {
  const [mode, setMode] = useState<Mode>(initial);

  return (
    <div className="grid min-h-dvh place-items-center px-3 py-4 sm:px-6 sm:py-10">
      <div className="grid w-full max-w-[1180px] gap-3 rounded-[40px] bg-frost p-3 shadow-raised ring-1 ring-white/60 backdrop-blur-xl sm:gap-4 sm:p-4 lg:grid-cols-[1.08fr_1fr] dark:ring-white/8">
        <Story />
        <section className="rounded-panel bg-surface px-5 py-6 sm:px-10 sm:py-10">
          <ModeSwitch mode={mode} onChange={setMode} />
          <div id={`panel-${mode}`} role="tabpanel" aria-labelledby={`tab-${mode}`} className="mt-8">
            {mode === "agent" ? <AgentSignIn /> : <CustomerStart />}
          </div>
        </section>
      </div>
    </div>
  );
}

/** The left panel: the brand, what this is, and a sample ticket stub. */
function Story() {
  return (
    <section className="relative flex flex-col overflow-hidden rounded-panel bg-[linear-gradient(160deg,#eee9f9_0%,#f1e8f4_55%,#f8e6ea_100%)] px-6 pt-6 pb-8 sm:px-10 sm:pt-10 lg:min-h-[640px] dark:bg-[linear-gradient(160deg,#1d1838,#211631)]">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-3">
          <LogoMark className="size-11 drop-shadow-[0_4px_10px_rgb(44_27_100/0.12)]" />
          <span className="text-[22px] font-bold tracking-[-0.03em] text-ink">ServiceMesh</span>
        </span>
        <ThemeToggle />
      </div>
      <h1 className="mt-10 max-w-[16ch] text-[34px] leading-[1.08] font-bold tracking-[-0.04em] text-ink sm:mt-16 sm:text-[44px]">
        Support that keeps up with your devices.
      </h1>
      <p className="mt-4 max-w-[38ch] text-[16px] leading-relaxed text-ink-secondary">
        Track repairs, chat with technicians and manage tickets in one place.
      </p>
      <div className="mt-10 flex justify-center lg:mt-auto lg:pt-12">
        <SampleStub />
      </div>
    </section>
  );
}

/** A sample ticket, as every ticket in the inbox looks: the page's one moving thing, on load. */
function SampleStub() {
  return (
    <div
      aria-hidden
      style={stubColours("urgent")}
      className="w-full max-w-[340px] animate-[stub-in_900ms_cubic-bezier(0.2,0.7,0.2,1)_200ms_both] overflow-hidden rounded-card shadow-[0_28px_50px_-22px_color-mix(in_oklab,var(--g1)_75%,transparent)]"
    >
      <StubTop className="px-5 pt-4 pb-5">
        <div className="flex items-center justify-between text-[13px] font-semibold">
          <span className="flex items-center gap-2">
            <span className="grid size-5 place-items-center rounded-full bg-white/30">
              <span className="size-2 rounded-full bg-white" />
            </span>
            ServiceMesh
          </span>
          <span className="text-white/85">Just now</span>
        </div>
        <div className="mt-5 flex items-center gap-2 text-[14px] font-semibold">
          <StubPriority label="High" />
          Lumen Book 13
        </div>
        <p className="mt-1 text-[30px] leading-none font-extrabold tracking-[-0.03em] tabular-nums">SR-2026-00042</p>
      </StubTop>
      <Perforation notch="var(--stub-notch)" />
      <div className="grid grid-cols-3 gap-2 bg-[linear-gradient(160deg,color-mix(in_oklab,var(--g1)_62%,white),color-mix(in_oklab,var(--g2)_58%,white))] px-5 pt-4 pb-4 text-white">
        {[["Channel", "Web chat"], ["Type", "Hardware"], ["Status", "New"]].map(([label, value], i) => (
          <div key={label} className={i === 2 ? "text-right" : i === 1 ? "text-center" : ""}>
            <p className="text-[10px] font-bold tracking-[0.08em] text-white/75 uppercase">{label}</p>
            <p className="text-[14px] font-semibold">{value}</p>
          </div>
        ))}
      </div>
    </div>
  );
}

const MODES: { id: Mode; label: string; icon: typeof UserRound }[] = [
  { id: "customer", label: "Customer", icon: UserRound },
  { id: "agent", label: "Agent", icon: Headphones },
];

/** The Customer | Agent switch: tabs, with the arrow keys moving between them. */
function ModeSwitch({ mode, onChange }: { mode: Mode; onChange: (mode: Mode) => void }) {
  const refs = useRef<Record<Mode, HTMLButtonElement | null>>({ customer: null, agent: null });

  function onKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const next: Mode = mode === "customer" ? "agent" : "customer";
    onChange(next);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label="Who are you?"
      onKeyDown={onKeyDown}
      className="relative grid grid-cols-2 rounded-full bg-canvas p-1.5 dark:bg-surface-raised"
    >
      {/* The indigo pill slides under the chosen side. */}
      <span
        aria-hidden
        className={`absolute inset-y-1.5 left-1.5 w-[calc(50%-6px)] rounded-full bg-accent shadow-[0_8px_18px_-8px_color-mix(in_oklab,var(--accent)_80%,transparent)] transition-transform duration-300 ease-[cubic-bezier(0.2,0.7,0.2,1)] ${
          mode === "agent" ? "translate-x-full" : "translate-x-0"
        }`}
      />
      {MODES.map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          ref={(node) => {
            refs.current[id] = node;
          }}
          id={`tab-${id}`}
          type="button"
          role="tab"
          aria-selected={mode === id}
          aria-controls={`panel-${id}`}
          tabIndex={mode === id ? 0 : -1}
          onClick={() => onChange(id)}
          className={`relative z-10 inline-flex h-11 items-center justify-center gap-2 rounded-full text-[15px] font-semibold outline-none transition-colors focus-visible:ring-2 focus-visible:ring-violet focus-visible:ring-offset-2 focus-visible:ring-offset-canvas ${
            mode === id ? "text-on-accent" : "text-ink-secondary hover:text-ink"
          }`}
        >
          <Icon className="size-[18px]" aria-hidden />
          {label}
        </button>
      ))}
    </div>
  );
}

/** Staff sign-in: work email and password, the only way in for staff. */
function AgentSignIn() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const ids = { email: useId(), password: useId(), keep: useId() };

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setPending(true);
    setError(null);
    try {
      const res = await api.login({ email: String(form.get("email")), password: String(form.get("password")) });
      setToken(res.access_token, form.get("keep") === "on");
      router.replace(HOME_BY_ROLE[res.user.role]);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Couldn’t reach the server. Check your connection and try again.");
      setPending(false);
    }
  }

  return (
    <div className="animate-in fade-in slide-in-from-right-2 duration-300">
      <h2 className="text-[30px] leading-tight font-bold tracking-[-0.035em] text-ink sm:text-[34px]">Agent sign in</h2>
      <p className="mt-2 text-[15px] text-ink-secondary">Use your work account to manage the support queue.</p>

      <form onSubmit={(event) => void onSubmit(event)} className="mt-8 space-y-5">
        <div className="space-y-2">
          <label htmlFor={ids.email} className="block text-[14px] font-medium text-ink">Work email</label>
          <input id={ids.email} name="email" type="email" autoComplete="username" required autoFocus
                 placeholder="you@yourcompany.com" className={FIELD} />
        </div>
        <div className="space-y-2">
          <label htmlFor={ids.password} className="block text-[14px] font-medium text-ink">Password</label>
          <div className="relative">
            <input id={ids.password} name="password" type={showPassword ? "text" : "password"}
                   autoComplete="current-password" required className={`${FIELD} pr-12`} />
            <button
              type="button"
              onClick={() => setShowPassword((shown) => !shown)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              aria-pressed={showPassword}
              className="absolute top-1/2 right-2 grid size-9 -translate-y-1/2 place-items-center rounded-full text-ink-secondary outline-none transition-colors hover:bg-canvas hover:text-ink focus-visible:ring-2 focus-visible:ring-violet dark:hover:bg-surface-raised"
            >
              {showPassword ? <EyeOff className="size-[18px]" aria-hidden /> : <Eye className="size-[18px]" aria-hidden />}
            </button>
          </div>
        </div>

        <label htmlFor={ids.keep} className="flex w-fit cursor-pointer items-center gap-2.5 text-[14px] text-ink-secondary">
          <input id={ids.keep} name="keep" type="checkbox" defaultChecked
                 className="size-[18px] cursor-pointer rounded-[5px] accent-(--accent)" />
          Keep me signed in
        </label>

        {error ? (
          <p role="alert" className="rounded-control bg-danger/10 px-4 py-3 text-[14px] text-danger">
            {error}
          </p>
        ) : null}

        <button
          type="submit"
          disabled={pending}
          className="inline-flex h-12 w-full items-center justify-center gap-2 rounded-control bg-accent text-[16px] font-semibold text-on-accent shadow-[0_14px_28px_-14px_color-mix(in_oklab,var(--accent)_90%,transparent)] outline-none transition hover:brightness-[1.15] active:scale-[0.99] focus-visible:ring-2 focus-visible:ring-violet focus-visible:ring-offset-2 focus-visible:ring-offset-surface disabled:opacity-70"
        >
          {pending ? <LoaderCircle className="size-[18px] animate-spin" aria-hidden /> : null}
          {pending ? "Signing in…" : "Sign in to dashboard"}
        </button>
      </form>

      <p className="mt-8 border-t border-hairline pt-6 text-[14px] text-ink-secondary">
        For service agents, technicians and warehouse staff. Your admin gives you an account.
      </p>
    </div>
  );
}

/** Customers: no account, just the support chat. */
function CustomerStart() {
  return (
    <div className="animate-in fade-in slide-in-from-left-2 duration-300">
      <h2 className="text-[30px] leading-tight font-bold tracking-[-0.035em] text-ink sm:text-[34px]">Get help with your device</h2>
      <p className="mt-2 text-[15px] text-ink-secondary">
        No account needed. Tell us what’s wrong and we’ll raise a ticket and reply right there.
      </p>

      <ul className="mt-8 space-y-3">
        {[
          ["Have your serial number ready", "It’s on the sticker under the laptop, e.g. AX14-7F3K92."],
          ["We’ll ask for your email", "So we can send you the ticket number and updates."],
        ].map(([title, text]) => (
          <li key={title} className="rounded-control bg-canvas px-4 py-3 dark:bg-surface-raised">
            <p className="text-[15px] font-semibold text-ink">{title}</p>
            <p className="text-[14px] text-ink-secondary">{text}</p>
          </li>
        ))}
      </ul>

      <Link
        href="/support"
        className="mt-8 inline-flex h-12 w-full items-center justify-center gap-2 rounded-control bg-accent text-[16px] font-semibold text-on-accent shadow-[0_14px_28px_-14px_color-mix(in_oklab,var(--accent)_90%,transparent)] outline-none transition hover:brightness-[1.15] active:scale-[0.99] focus-visible:ring-2 focus-visible:ring-violet focus-visible:ring-offset-2 focus-visible:ring-offset-surface"
      >
        <MessageCircle className="size-[18px]" aria-hidden />
        Start a chat
      </Link>

      <p className="mt-8 flex flex-wrap items-center gap-2 border-t border-hairline pt-6 text-[14px] text-ink-secondary">
        Or write to us on
        {(["discord", "telegram", "email"] as const).map((channel) => (
          <span key={channel} className="inline-flex items-center gap-1.5 rounded-full bg-canvas px-2.5 py-1 font-medium text-ink dark:bg-surface-raised">
            <ChannelGlyph channel={channel} /> {channel === "email" ? "Email" : channel[0].toUpperCase() + channel.slice(1)}
          </span>
        ))}
      </p>
    </div>
  );
}
