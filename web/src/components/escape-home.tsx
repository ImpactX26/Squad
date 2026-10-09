"use client";

// Escape goes back to the home page (/) from the entry pages (login, support). A key press that another
// element already handled (a menu or dialog closing) is left alone.

import { useRouter } from "next/navigation";
import { useEffect } from "react";

export function EscapeHome() {
  const router = useRouter();
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape" && !event.defaultPrevented) router.push("/");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);
  return null;
}
