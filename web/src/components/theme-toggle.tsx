"use client";

import { useSyncExternalStore } from "react";
import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { ToggleGroup } from "radix-ui";

import { cn } from "@/lib/utils";

const OPTIONS = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "system", label: "System", Icon: Monitor },
] as const;

const noopSubscribe = () => () => {};

/** Light / dark / system, as a segmented control. */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  // The chosen theme is only known in the browser: show no selection until hydrated.
  const hydrated = useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );

  return (
    <ToggleGroup.Root
      type="single"
      aria-label="Appearance"
      value={hydrated ? (theme ?? "system") : ""}
      onValueChange={(next) => {
        // Clicking the selected option would clear it; keep one selected.
        if (next) setTheme(next);
      }}
      className={cn("inline-flex items-center gap-0.5 rounded-full bg-surface p-0.5", className)}
    >
      {OPTIONS.map(({ value, label, Icon }) => (
        <ToggleGroup.Item
          key={value}
          value={value}
          aria-label={label}
          title={label}
          className="inline-flex size-9 items-center justify-center rounded-full text-ink-secondary transition-colors hover:text-ink data-[state=on]:bg-hairline data-[state=on]:text-ink"
        >
          <Icon aria-hidden="true" className="size-4" />
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
