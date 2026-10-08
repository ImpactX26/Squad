import { Brand } from "@/components/brand";
import { ThemeToggle } from "@/components/theme-toggle";

// Step 1 of Block 1: a check of the §11.3 tokens in both themes.
// The home page of §11.2 replaces this in the next commit.

const COLORS = [
  { name: "canvas", swatch: "bg-canvas" },
  { name: "surface", swatch: "bg-surface" },
  { name: "surface-raised", swatch: "bg-surface-raised shadow-raised" },
  { name: "ink", swatch: "bg-ink" },
  { name: "ink-secondary", swatch: "bg-ink-secondary" },
  { name: "hairline", swatch: "bg-hairline" },
  { name: "accent", swatch: "bg-accent" },
  { name: "on-accent", swatch: "bg-on-accent" },
  { name: "success", swatch: "bg-success" },
  { name: "warning", swatch: "bg-warning" },
  { name: "danger", swatch: "bg-danger" },
];

const TYPE = [
  { name: "large-title · 34", sample: "text-large-title font-semibold" },
  { name: "title-1 · 28", sample: "text-title-1 font-semibold" },
  { name: "title-2 · 22", sample: "text-title-2 font-semibold" },
  { name: "body · 17", sample: "text-body" },
  { name: "subheadline · 15", sample: "text-subheadline" },
  { name: "footnote · 13", sample: "text-footnote" },
];

const RADII = [
  { name: "panel · 20", shape: "rounded-panel" },
  { name: "card · 14", shape: "rounded-card" },
  { name: "control · 10", shape: "rounded-control" },
  { name: "pill", shape: "rounded-full" },
];

export default function TokensPage() {
  return (
    <div className="mx-auto max-w-5xl px-4 pb-16 sm:px-6">
      <header className="flex h-16 items-center justify-between">
        <Brand />
        <ThemeToggle />
      </header>

      <main className="mt-6 space-y-12">
        <div>
          <h1 className="text-large-title font-semibold">Design tokens</h1>
          <p className="mt-2 text-ink-secondary">
            Switch the appearance at the top to check light and dark.
          </p>
        </div>

        <section aria-labelledby="colors">
          <h2 id="colors" className="text-title-2 font-semibold">Colors</h2>
          <ul className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
            {COLORS.map(({ name, swatch }) => (
              <li key={name} className="rounded-card bg-surface p-3">
                <div className={`h-14 rounded-control ring-1 ring-hairline ${swatch}`} />
                <p className="mt-2 text-footnote text-ink-secondary">{name}</p>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="type">
          <h2 id="type" className="text-title-2 font-semibold">Type</h2>
          <ul className="mt-4 divide-y divide-hairline rounded-card bg-surface">
            {TYPE.map(({ name, sample }) => (
              <li key={name} className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 px-4 py-3">
                <span className={sample}>Battery not charging</span>
                <span className="text-footnote text-ink-secondary tabular-nums">{name}</span>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="shape">
          <h2 id="shape" className="text-title-2 font-semibold">Radii</h2>
          <ul className="mt-4 flex flex-wrap gap-4">
            {RADII.map(({ name, shape }) => (
              <li key={name} className="text-center">
                <div className={`h-16 w-24 bg-surface ring-1 ring-hairline ${shape}`} />
                <p className="mt-2 text-footnote text-ink-secondary">{name}</p>
              </li>
            ))}
          </ul>
        </section>
      </main>
    </div>
  );
}
