import type { Role } from "@/lib/auth";

export type NavItem = { href: string; label: string; roles: readonly Role[] };

/**
 * The top bar's pages (§11.2, §11.3), in order. Only pages that exist are listed; each block
 * adds its own (Copilot, Payments, Commands, Inventory, Jobs) as it builds them.
 */
export const NAV: readonly NavItem[] = [{ href: "/inbox", label: "Inbox", roles: ["agent", "admin"] }];

/** Who may open a staff page, by path prefix. Pages not listed are open to any signed-in role. */
export function rolesFor(pathname: string): readonly Role[] | null {
  return NAV.find((item) => pathname === item.href || pathname.startsWith(`${item.href}/`))?.roles ?? null;
}
