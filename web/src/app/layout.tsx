import type { Metadata } from "next";
import { ThemeProvider } from "next-themes";

import { COMPANY_NAME } from "@/components/brand";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: `${COMPANY_NAME} support`, template: `%s · ${COMPANY_NAME}` },
  description: `Help for your ${COMPANY_NAME} laptop, PC or headphones.`,
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    // next-themes sets the class on <html> before React hydrates.
    <html lang="en" suppressHydrationWarning>
      <body className="min-h-dvh">
        <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
          {children}
        </ThemeProvider>
      </body>
    </html>
  );
}
