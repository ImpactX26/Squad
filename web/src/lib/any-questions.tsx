"use client";

// The "Any Questions" sidebar's state (ARCHITECTURE.md §11.2): open or closed, and its width.
// It lives in the staff layout, so the chat survives moving between pages, and both choices are
// remembered in this browser. Ctrl + . (⌘ . on a Mac) toggles it from anywhere. Ctrl + W can't be
// used: browsers keep it for closing the tab and never pass it to the page.

import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useState } from "react";

export const PANEL_MIN = 320;
export const PANEL_DEFAULT = 400;
export const PANEL_MAX = 720;
// Wide enough for the inbox and the panel side by side; below it the panel opens as a sheet.
export const SIDE_BY_SIDE = 1024;

const OPEN_KEY = "any-questions:open";
const WIDTH_KEY = "any-questions:width";

type AnyQuestions = {
  /** Agents and admins only; for anyone else the panel and its shortcut don't exist. */
  available: boolean;
  open: boolean;
  setOpen: (open: boolean) => void;
  toggle: () => void;
  width: number;
  setWidth: (width: number) => void;
  /** "Ctrl ." or "⌘ .", for the hint next to the button. */
  shortcut: string;
};

const AnyQuestionsContext = createContext<AnyQuestions>({
  available: false,
  open: false,
  setOpen: () => {},
  toggle: () => {},
  width: PANEL_DEFAULT,
  setWidth: () => {},
  shortcut: "Ctrl .",
});

export function useAnyQuestions(): AnyQuestions {
  return useContext(AnyQuestionsContext);
}

/** Clamp to the allowed range, and never more than 60% of the window. */
export function clampWidth(width: number): number {
  const ceiling = typeof window === "undefined" ? PANEL_MAX : Math.min(PANEL_MAX, Math.round(window.innerWidth * 0.6));
  return Math.round(Math.min(Math.max(width, PANEL_MIN), Math.max(PANEL_MIN, ceiling)));
}

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // private window or blocked storage: fall back to the defaults
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Remembering is a convenience; the panel works without it.
  }
}

// The staff layout renders this only in the browser, once the session check has answered, so the
// initial state can read the browser's remembered choices directly (no server render to mismatch).
function initialOpen(): boolean {
  const stored = read(OPEN_KEY);
  // First visit: open beside the inbox on a wide screen, closed on a small one.
  return stored === null ? window.innerWidth >= 1280 : stored === "1";
}

function initialWidth(): number {
  const stored = Number(read(WIDTH_KEY));
  return stored ? clampWidth(stored) : PANEL_DEFAULT;
}

export function AnyQuestionsProvider({ available, children }: { available: boolean; children: ReactNode }) {
  const [open, setOpenState] = useState(initialOpen);
  const [width, setWidthState] = useState(initialWidth);
  const shortcut = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent) ? "⌘ ." : "Ctrl .";

  const setOpen = useCallback((next: boolean) => {
    setOpenState(next);
    write(OPEN_KEY, next ? "1" : "0");
  }, []);

  const toggle = useCallback(() => {
    setOpenState((current) => {
      write(OPEN_KEY, current ? "0" : "1");
      return !current;
    });
  }, []);

  const setWidth = useCallback((next: number) => {
    const clamped = clampWidth(next);
    setWidthState(clamped);
    write(WIDTH_KEY, String(clamped));
  }, []);

  useEffect(() => {
    if (!available) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "." && (event.ctrlKey || event.metaKey) && !event.altKey && !event.shiftKey) {
        event.preventDefault();
        toggle();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [available, toggle]);

  const value = useMemo<AnyQuestions>(
    () => ({ available, open: available && open, setOpen, toggle, width, setWidth, shortcut }),
    [available, open, setOpen, toggle, width, setWidth, shortcut],
  );
  return <AnyQuestionsContext value={value}>{children}</AnyQuestionsContext>;
}
