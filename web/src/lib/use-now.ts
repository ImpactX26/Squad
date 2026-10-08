"use client";

import { useEffect, useState } from "react";

/** The current time, refreshed every `ms`, so relative times ("2m ago") stay true. */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(timer);
  }, [ms]);
  return now;
}
