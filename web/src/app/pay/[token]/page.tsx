import type { Metadata } from "next";

import { env } from "@/lib/env";

import { PayPage } from "./pay-page";

// The token in this URL is the only key to the invoice (section 7.6): keep it out of search
// engines, and out of the Referer of anything the page links to.
export const metadata: Metadata = {
  title: `Pay your invoice · ${env.companyName}`,
  robots: { index: false, follow: false },
  referrer: "no-referrer",
};

export default async function Page({ params }: PageProps<"/pay/[token]">) {
  const { token } = await params;
  return <PayPage token={token} companyName={env.companyName} />;
}
