"use client";

import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useSyncExternalStore } from "react";

import { Button } from "@/components/ui/button";

const ORDER = ["system", "light", "dark"] as const;
type Theme = (typeof ORDER)[number];

const META: Record<Theme, { icon: typeof Sun; label: string }> = {
  system: { icon: Monitor, label: "System" },
  light: { icon: Sun, label: "Light" },
  dark: { icon: Moon, label: "Dark" },
};

const noop = () => () => {};

/** Cycles system → light → dark. Renders the system icon until mounted, since the server can't know the theme. */
export function ThemeToggle() {
  const { theme, setTheme } = useTheme();
  const mounted = useSyncExternalStore(noop, () => true, () => false);
  const current: Theme = mounted && ORDER.includes(theme as Theme) ? (theme as Theme) : "system";
  const next = ORDER[(ORDER.indexOf(current) + 1) % ORDER.length];
  const Icon = META[current].icon;

  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(next)}
      disabled={!mounted}
      aria-label={`Theme: ${META[current].label}. Switch to ${META[next].label.toLowerCase()}.`}
      title={`Theme: ${META[current].label}`}
      className="text-ink-secondary hover:text-ink"
    >
      <Icon />
    </Button>
  );
}
