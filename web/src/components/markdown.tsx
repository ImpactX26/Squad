// Model-written Markdown, rendered with the §11.3 tokens (light and dark): paragraphs, lists, bold,
// inline code, links, and GitHub-style tables (remark-gfm). react-markdown renders no raw HTML, so
// whatever a model writes can't inject markup. A wide table scrolls inside its own box, never the page.

import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const components: Components = {
  p: ({ children }) => <p className="text-body text-ink">{children}</p>,
  ul: ({ children }) => <ul className="list-disc space-y-1 pl-5 text-body text-ink">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal space-y-1 pl-5 text-body text-ink">{children}</ol>,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  a: ({ children, href }) => (
    <a href={href} className="text-accent hover:underline" target="_blank" rel="noreferrer">
      {children}
    </a>
  ),
  code: ({ children }) => (
    <code className="rounded bg-canvas px-1 py-0.5 text-[0.9em] tabular-nums dark:bg-surface-raised">{children}</code>
  ),
  h1: ({ children }) => <p className="text-body font-semibold text-ink">{children}</p>,
  h2: ({ children }) => <p className="text-body font-semibold text-ink">{children}</p>,
  h3: ({ children }) => <p className="text-subheadline font-semibold text-ink">{children}</p>,
  table: ({ children }) => (
    <div className="max-w-full overflow-x-auto rounded-control border border-hairline">
      <table className="w-full border-collapse text-left text-subheadline tabular-nums">{children}</table>
    </div>
  ),
  thead: ({ children }) => <thead className="bg-canvas dark:bg-surface-raised">{children}</thead>,
  th: ({ children }) => (
    <th scope="col" className="whitespace-nowrap border-b border-hairline px-3 py-2 text-footnote font-semibold text-ink-secondary">
      {children}
    </th>
  ),
  tr: ({ children }) => <tr className="border-b border-hairline last:border-b-0">{children}</tr>,
  td: ({ children }) => <td className="whitespace-nowrap px-3 py-2 align-top text-ink">{children}</td>,
};

export function Markdown({ text }: { text: string }) {
  return (
    <div className="space-y-3">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
