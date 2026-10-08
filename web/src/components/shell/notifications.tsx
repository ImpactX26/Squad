"use client";

import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

/**
 * The bell's notifications (§9 notification.created). There is no list endpoint in §10, so the
 * bell holds the ones that arrive while this page is open; /ws/staff feeds it.
 */
export type Notification = {
  id: string;
  title: string;
  body: string | null;
  link: string | null;
  createdAt: string;
  read: boolean;
};

type NotificationsValue = {
  items: Notification[];
  unread: number;
  add: (item: Omit<Notification, "read">) => void;
  markAllRead: () => void;
};

const NotificationsContext = createContext<NotificationsValue | null>(null);

const KEEP = 50;

export function NotificationsProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Notification[]>([]);

  const add = useCallback((item: Omit<Notification, "read">) => {
    setItems((current) =>
      current.some((n) => n.id === item.id) ? current : [{ ...item, read: false }, ...current].slice(0, KEEP),
    );
  }, []);
  const markAllRead = useCallback(() => setItems((current) => current.map((n) => ({ ...n, read: true }))), []);

  const value = useMemo(
    () => ({ items, unread: items.filter((n) => !n.read).length, add, markAllRead }),
    [items, add, markAllRead],
  );
  return <NotificationsContext.Provider value={value}>{children}</NotificationsContext.Provider>;
}

export function useNotifications(): NotificationsValue {
  const value = useContext(NotificationsContext);
  if (!value) throw new Error("useNotifications needs a NotificationsProvider");
  return value;
}
