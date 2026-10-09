// "Save as PDF" for one copilot answer: a new window with only that answer, styled for print, then
// the browser's print dialog (choose "Save as PDF" there). The answer goes through the same Markdown
// component as on screen, rendered by React into the new window, so it stays escaped text: no HTML
// string is ever built from it. The window has its own light-only stylesheet and none of the app's,
// so the app's theme doesn't reach the page.

import { flushSync } from "react-dom";
import { createRoot } from "react-dom/client";

import { Markdown } from "@/components/markdown";
import { env } from "@/lib/env";
import { absolute } from "@/lib/format";

export const PRINT_FOOTER = "Generated from live system data by the support copilot";

// Light only. The Markdown component's Tailwind classes have no stylesheet here, so the answer is
// styled by element; its headings are <p class="… font-semibold …">, matched by that class.
const PRINT_CSS = `
@page { size: A4; margin: 16mm 14mm 18mm; }
:root { color-scheme: light; }
* { box-sizing: border-box; }
html, body { margin: 0; background: #fff; color: #1d1d1f; }
body {
  font: 10.5pt/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  -webkit-print-color-adjust: exact; print-color-adjust: exact;
}
/* On screen the text column is as wide as the printed one (A4 less the @page margins), so a table
   measured here fits the same way on paper. */
.sheet { max-width: calc(182mm + 32px); margin: 0 auto; padding: 24px 16px; }
@media print { .sheet { max-width: none; padding: 0; } }
.head { display: flex; align-items: center; gap: 10px; padding-bottom: 10px; margin-bottom: 14px;
        border-bottom: 1px solid #d2d2d7; }
.head img { width: 28px; height: 28px; }
.head .name { font-size: 13pt; font-weight: 600; letter-spacing: -0.01em; }
.head .when { margin-left: auto; text-align: right; font-size: 9pt; color: #6e6e73; }
.answer p, .answer ul, .answer ol { margin: 0 0 8px; }
.answer ul, .answer ol { padding-left: 20px; }
.answer li + li { margin-top: 2px; }
.answer p[class~="font-semibold"] { margin-top: 12px; font-weight: 600; break-after: avoid; }
.answer strong { font-weight: 600; }
.answer a { color: #0066cc; text-decoration: none; }
.answer code { font-family: ui-monospace, Consolas, Menlo, monospace; font-size: 0.9em;
               background: #f5f5f7; border-radius: 3px; padding: 0 3px; }
.answer table { width: 100%; border-collapse: collapse; margin: 4px 0 10px; table-layout: auto;
                font-size: 9pt; font-variant-numeric: tabular-nums; }
.answer thead { display: table-header-group; }
.answer th { background: #f5f5f7; font-weight: 600; text-align: left; color: #3a3a3c; }
.answer th, .answer td { border: 1px solid #d2d2d7; padding: 4px 6px; vertical-align: top;
                         white-space: normal; overflow-wrap: break-word; }
/* Only for a table whose whole words are wider than the page: break anywhere rather than overflow. */
.answer table.tight th, .answer table.tight td { overflow-wrap: anywhere; }
.answer tr { break-inside: avoid; }
.foot { break-before: avoid; break-inside: avoid; margin-top: 18px; padding-top: 8px; border-top: 1px solid #d2d2d7; font-size: 8.5pt; color: #6e6e73; }
`;

function PrintedAnswer({ text, answeredAt, logo }: { text: string; answeredAt: string; logo: string | null }) {
  return (
    <div className="sheet">
      <header className="head">
        {/* A plain img: next/image needs the app around it, and this window has none. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        {logo ? <img src={logo} alt="" /> : null}
        <span className="name">{env.companyName}</span>
        <span className="when">
          Any Questions answer
          <br />
          <time dateTime={answeredAt}>{absolute(answeredAt)}</time>
        </span>
      </header>
      <main className="answer">
        <Markdown text={text} />
      </main>
      <footer className="foot">{PRINT_FOOTER}</footer>
    </div>
  );
}

/** Opens the print window for one answer. False when the browser blocked the window. */
export function printAnswer(text: string, answeredAt: string): boolean {
  const win = window.open("", "_blank", "width=900,height=1100");
  if (!win) return false;
  const logo = document.querySelector<HTMLLinkElement>('link[rel="icon"]')?.href ?? null;

  // A fixed skeleton, so the page is in standards mode; nothing from the answer is written here.
  const doc = win.document;
  doc.open();
  doc.write('<!doctype html><html lang="en"><head><meta charset="utf-8"></head><body></body></html>');
  doc.close();
  doc.title = `${env.companyName} copilot answer, ${absolute(answeredAt)}`;
  const style = doc.createElement("style");
  style.textContent = PRINT_CSS;
  doc.head.append(style);

  const mount = doc.createElement("div");
  doc.body.append(mount);
  const root = createRoot(mount);
  flushSync(() => root.render(<PrintedAnswer text={text} answeredAt={answeredAt} logo={logo} />));
  win.addEventListener("pagehide", () => root.unmount(), { once: true });
  // Words stay whole in table cells unless the table would then be wider than the page.
  for (const table of doc.querySelectorAll<HTMLTableElement>(".answer table")) {
    const box = table.parentElement;
    if (box && table.offsetWidth > box.clientWidth) table.classList.add("tight");
  }

  // Print once the logo has loaded (or failed), so it is on the page.
  const image = doc.querySelector("img");
  const ready = image ? image.decode().catch(() => undefined) : Promise.resolve();
  void ready.then(() => {
    win.focus();
    win.print();
  });
  return true;
}
