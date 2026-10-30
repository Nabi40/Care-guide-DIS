"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import type { Stats } from "@/lib/types";

interface AppState {
  stats: Stats | null;
  /** Bumped whenever server data may have changed; pages reload when it does. */
  version: number;
  refresh: () => void;
  toast: (message: string) => void;
}

const AppContext = createContext<AppState | null>(null);

export function useApp(): AppState {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp must be used inside AppProvider");
  return ctx;
}

const POLL_MS = 1500;

export function AppProvider({ children }: { children: ReactNode }) {
  const [stats, setStats] = useState<Stats | null>(null);
  const [version, setVersion] = useState(0);
  const [message, setMessage] = useState("");
  const toastTimer = useRef<ReturnType<typeof setTimeout>>(undefined);

  const refresh = useCallback(() => setVersion((v) => v + 1), []);
  const toast = useCallback((text: string) => {
    setMessage(text);
    clearTimeout(toastTimer.current);
    toastTimer.current = setTimeout(() => setMessage(""), 3500);
  }, []);

  // Poll only while something is in flight.
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    api<Stats>("/api/stats")
      .then((s) => {
        if (cancelled) return;
        setStats(s);
        if ((s.documents.queued ?? 0) + (s.documents.processing ?? 0) > 0) timer = setTimeout(refresh, POLL_MS);
      })
      .catch(() => {
        /* the page that needs the data shows its own error */
      });
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [version, refresh]);

  const value = useMemo(() => ({ stats, version, refresh, toast }), [stats, version, refresh, toast]);
  return (
    <AppContext.Provider value={value}>
      {children}
      <div id="toast" className={message ? "show" : ""} role="status" aria-live="polite">
        {message}
      </div>
    </AppContext.Provider>
  );
}
