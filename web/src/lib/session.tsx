"use client";

import { createContext, useContext } from "react";

import type { StaffUser } from "@/lib/api";

/** The signed-in staff user, provided by the staff layout once GET /api/me has answered. */
export const StaffUserContext = createContext<StaffUser | null>(null);

export function useStaffUser(): StaffUser | null {
  return useContext(StaffUserContext);
}
