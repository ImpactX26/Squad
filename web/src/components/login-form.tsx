"use client";

import { AlertCircle, LoaderCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ApiError, api } from "@/lib/api";
import { homeFor, safeNext, saveSession, useSession, type Session } from "@/lib/auth";

function nextPath(): string | null {
  return safeNext(new URLSearchParams(window.location.search).get("next"));
}

export function LoginForm() {
  const router = useRouter();
  const session = useSession();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Already signed in (another tab, or back here by mistake): go straight on.
  useEffect(() => {
    if (session && !busy) router.replace(nextPath() ?? homeFor(session.staff.role));
  }, [session, busy, router]);

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const signedIn = await api<Session>("/api/auth/login", {
        method: "POST",
        body: { email, password },
        auth: false,
      });
      saveSession(signedIn);
      router.replace(nextPath() ?? homeFor(signedIn.staff.role));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Something went wrong. Try again.");
      setBusy(false);
    }
  }

  return (
    <form onSubmit={onSubmit} className="mt-6 space-y-4" noValidate>
      <div>
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          type="email"
          autoComplete="username"
          inputMode="email"
          required
          autoFocus
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          aria-invalid={error ? true : undefined}
          placeholder="name@example.com"
        />
      </div>
      <div>
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          type="password"
          autoComplete="current-password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          aria-invalid={error ? true : undefined}
        />
      </div>

      <div aria-live="polite" className="min-h-0">
        {error && (
          <p className="flex items-start gap-2 text-subheadline text-danger">
            <AlertCircle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
            {error}
          </p>
        )}
      </div>

      <Button type="submit" className="w-full" disabled={busy || !email.trim() || !password}>
        {busy && <LoaderCircle aria-hidden="true" className="animate-spin" />}
        {busy ? "Signing in" : "Sign in"}
      </Button>
    </form>
  );
}
