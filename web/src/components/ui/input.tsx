import type { ComponentProps } from "react";

import { cn } from "@/lib/utils";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-11 w-full rounded-control bg-surface px-3.5 text-body text-ink ring-1 ring-hairline ring-inset transition-shadow placeholder:text-ink-secondary focus:ring-2 focus:ring-accent focus:outline-none focus-visible:outline-none disabled:opacity-60 aria-invalid:ring-danger",
        className,
      )}
      {...props}
    />
  );
}

export function Label({ className, ...props }: ComponentProps<"label">) {
  return <label className={cn("mb-1.5 block text-subheadline font-semibold text-ink", className)} {...props} />;
}
