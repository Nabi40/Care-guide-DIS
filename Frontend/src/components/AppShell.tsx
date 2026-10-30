"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";
import { setApiKey } from "@/lib/api";
import { useApp } from "./AppContext";

const LINKS = [
  { href: "/", label: "Documents" },
  { href: "/review", label: "Review queue" },
  { href: "/search", label: "Search" },
  { href: "/cross-check", label: "Cross-check" },
];

function Stats() {
  const { stats: s } = useApp();
  if (!s) return <div id="stats" className="stats" />;
  const d = s.documents;
  return (
    <div id="stats" className="stats" aria-live="polite">
      <span>Documents <b>{s.documents_total}</b></span>
      <span>Processing <b>{(d.queued ?? 0) + (d.processing ?? 0)}</b></span>
      <span>Failed <b>{d.failed ?? 0}</b></span>
      <span>Success rate <b>{s.success_rate === null ? "–" : `${(s.success_rate * 100).toFixed(1)}%`}</b></span>
      <span>Avg time <b>{s.avg_processing_ms ? `${(s.avg_processing_ms / 1000).toFixed(1)}s` : "–"}</b></span>
      <span title={s.llm_model ?? "Set LLM_ENABLED=true on the backend (needs Ollama) to enable"}>
        AI extraction <b>{s.llm_enabled ? "on" : "off"}</b>
      </span>
      <span title={s.embedding_model}>
        Search <b>{s.semantic_embeddings ? "semantic" : "fuzzy only"}</b>
      </span>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { stats, refresh } = useApp();
  const changeKey = () => {
    const key = window.prompt("API key:");
    if (key !== null) {
      setApiKey(key);
      refresh();
    }
  };
  return (
    <>
      <header className="top">
        <h1>Document Intelligence</h1>
        <Stats />
        <button className="link" onClick={changeKey}>API key…</button>
      </header>
      <nav className="tabs" aria-label="Sections">
        {LINKS.map((l) => (
          <Link key={l.href} href={l.href} className={pathname === l.href ? "active" : ""}>
            {l.label}
            {l.href === "/review" && stats?.needs_review ? <span className="badge warn">{stats.needs_review}</span> : null}
          </Link>
        ))}
      </nav>
      <main>{children}</main>
    </>
  );
}
