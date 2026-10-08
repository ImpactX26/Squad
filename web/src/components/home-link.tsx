"use client";

import { ChevronLeft } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { cn } from "@/lib/utils";

/** "Home", and Escape does the same (§11.2: /login and /support). */
export function HomeLink({ className }: { className?: string }) {
  const router = useRouter();

  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      // A menu or dialog that handled Escape first keeps the page.
      if (event.key === "Escape" && !event.defaultPrevented && !event.isComposing) router.push("/");
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [router]);

  return (
    <Link
      href="/"
      className={cn(
        "inline-flex h-9 items-center gap-1 rounded-full pr-3 pl-2 text-subheadline text-ink-secondary transition-colors hover:bg-surface hover:text-ink",
        className,
      )}
    >
      <ChevronLeft aria-hidden="true" className="size-4" />
      Home
    </Link>
  );
}
