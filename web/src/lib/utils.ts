import { clsx, type ClassValue } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

// tailwind-merge only knows Tailwind's default names. Without these, a token
// size like text-footnote reads as a text color and cn("text-ink", "text-footnote")
// silently drops text-ink. Keep in step with src/styles/tokens.css.
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: ["large-title", "title-1", "title-2", "body", "subheadline", "footnote"],
      radius: ["panel", "card", "control"],
      shadow: ["raised"],
    },
  },
});

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
