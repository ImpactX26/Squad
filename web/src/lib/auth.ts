/**
 * The signed-in staff member, kept in localStorage so a reload or a new tab stays signed in.
 *
 * The token is readable by the page because the API wants it as a Bearer header and /ws/staff
 * as ?token=. It is dropped on sign out, on a 401 or a 1008 close, and once expires_at passes.
 */

import { useSyncExternalStore } from "react";

import type { components } from "@/lib/api-types";

export type Staff = components["schemas"]["StaffOut"];
export type Session = components["schemas"]["TokenOut"];
export type Role = Staff["role"];

const KEY = "servicemesh.session";
const CHANGED = "servicemesh:session";

let cachedRaw: string | null | undefined;
let cachedSession: Session | null = null;

function read(): string | null {
  try {
    return window.localStorage.getItem(KEY);
  } catch {
    return null; // storage blocked: nobody is signed in
  }
}

export function getSession(): Session | null {
  if (typeof window === "undefined") return null;
  const raw = read();
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    try {
      cachedSession = raw ? (JSON.parse(raw) as Session) : null;
    } catch {
      cachedSession = null;
    }
  }
  if (cachedSession && Date.parse(cachedSession.expires_at) <= Date.now()) {
    clearSession();
    return null;
  }
  return cachedSession;
}

export function saveSession(session: Session): void {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(session));
  } catch {
    // Storage blocked: the session lasts until the page closes.
    cachedRaw = JSON.stringify(session);
    cachedSession = session;
  }
  window.dispatchEvent(new Event(CHANGED));
}

export function clearSession(): void {
  try {
    window.localStorage.removeItem(KEY);
  } catch {}
  cachedRaw = null;
  cachedSession = null;
  window.dispatchEvent(new Event(CHANGED));
}

let signedOutOnPurpose = false;

/** Sign out from the menu: the staff pages then go to /login with no ?next= to come back to. */
export function signOut(): void {
  signedOutOnPurpose = true;
  clearSession();
}

/** True once after signOut(); false when the session ended by itself (expiry, 401, 1008). */
export function takeSignOut(): boolean {
  const was = signedOutOnPurpose;
  signedOutOnPurpose = false;
  return was;
}

function subscribe(onChange: () => void): () => void {
  // "storage" fires when another tab signs in or out.
  window.addEventListener(CHANGED, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(CHANGED, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/** The session, or null; undefined while the page hasn't hydrated (the server can't know). */
export function useSession(): Session | null | undefined {
  return useSyncExternalStore(subscribe, getSession, () => undefined);
}

/** Where each role lands after signing in (§11.2). */
export function homeFor(role: Role): string {
  switch (role) {
    case "technician":
      return "/jobs";
    case "warehouse":
      return "/inventory";
    default:
      return "/inbox";
  }
}

/** A same-site path from ?next=, never an address elsewhere. */
export function safeNext(value: string | null): string | null {
  return value && value.startsWith("/") && !value.startsWith("//") ? value : null;
}

export const ROLE_LABEL: Record<Role, string> = {
  agent: "Service agent",
  technician: "Technician",
  warehouse: "Warehouse",
  admin: "Admin",
};
