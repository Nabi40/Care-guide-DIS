"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import type { DocStatus, DocumentSummary } from "@/lib/types";
import { useApp } from "./AppContext";

export const statusClass = (s: DocStatus): string =>
  ({ completed: "ok", failed: "err", processing: "info", queued: "" })[s];

interface Props {
  selected: string | null;
  onSelect: (id: string) => void;
}

export function DocumentList({ selected, onSelect }: Props) {
  const { stats, version, refresh, toast } = useApp();
  const [items, setItems] = useState<DocumentSummary[] | null>(null);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    let cancelled = false;
    const timer = setTimeout(() => {
      const params = new URLSearchParams({ limit: "100" });
      if (status) params.set("status", status);
      if (q.trim()) params.set("q", q.trim());
      api<{ items: DocumentSummary[] }>(`/api/documents?${params}`)
        .then((r) => !cancelled && setItems(r.items))
        .catch((e: Error) => !cancelled && toast(`Could not load documents: ${e.message}`));
    }, 150);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [status, q, version, toast]);

  async function retryFailed() {
    const r = await api<{ requeued: string[] }>("/api/documents/reprocess-failed", { method: "POST" });
    toast(`Re-queued ${r.requeued.length} document(s)`);
    refresh();
  }

  return (
    <>
      <div className="toolbar">
        <select id="statusFilter" aria-label="Filter by status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          <option>queued</option>
          <option>processing</option>
          <option>completed</option>
          <option>failed</option>
        </select>
        <input id="docQ" type="search" placeholder="Filter by file name" value={q} onChange={(e) => setQ(e.target.value)} />
        {stats?.documents.failed ? (
          <button id="retryFailed" className="btn small" onClick={retryFailed}>Reprocess failed</button>
        ) : null}
      </div>
      <ul id="docList" className="doclist">
        {items?.length === 0 ? <li className="muted pad">No documents yet.</li> : null}
        {items?.map((d) => (
          <li
            key={d.id}
            className={d.id === selected ? "doc sel" : "doc"}
            tabIndex={0}
            onClick={() => onSelect(d.id)}
            onKeyDown={(e) => e.key === "Enter" && onSelect(d.id)}
          >
            <div className="row">
              <span className="name" title={d.filename}>{d.filename}</span>
              <span className={`badge ${statusClass(d.status)}`}>{d.status}</span>
            </div>
            <div className="row muted">
              <span>
                {d.status === "failed"
                  ? (d.error_code ?? "failed")
                  : `${d.invoice_count} invoice(s)${d.page_count ? ` · ${d.page_count} p` : ""}`}
              </span>
              {d.review_count ? <span className="badge warn">{d.review_count} to review</span> : null}
            </div>
            {d.status === "processing" || d.status === "queued" ? (
              <div className="bar"><i style={{ width: `${Math.round(d.progress * 100)}%` }} /></div>
            ) : null}
          </li>
        ))}
      </ul>
    </>
  );
}
