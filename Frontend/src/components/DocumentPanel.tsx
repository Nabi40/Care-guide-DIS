"use client";

import { useCallback, useEffect, useState } from "react";
import { api, downloadFile } from "@/lib/api";
import type { DocumentDetail, PageText } from "@/lib/types";
import { useApp } from "./AppContext";
import { statusClass } from "./DocumentList";
import { InvoiceCard } from "./InvoiceCard";

const isActive = (d: DocumentDetail) => d.status === "queued" || d.status === "processing";

interface Props {
  id: string;
  onDeleted: () => void;
}

export function DocumentPanel({ id, onDeleted }: Props) {
  const { version, refresh, toast } = useApp();
  const [doc, setDoc] = useState<DocumentDetail | null>(null);
  const [revision, setRevision] = useState(0); // remounts invoice forms with fresh values
  const [text, setText] = useState<{ pages: PageText[]; page_count: number } | null>(null);

  const load = useCallback(async () => {
    const d = await api<DocumentDetail>(`/api/documents/${id}`);
    setDoc(d);
    setRevision((r) => r + 1);
  }, [id]);

  useEffect(() => {
    setText(null);
    load().catch((e: Error) => toast(e.message));
  }, [load, toast]);

  // Background polling replaces the view only while the document is moving,
  // so edits in progress are never wiped.
  useEffect(() => {
    if (!version) return;
    api<DocumentDetail>(`/api/documents/${id}`)
      .then((d) => {
        setDoc((prev) => {
          if (prev && prev.id === d.id && prev.status === d.status && !isActive(d)) return prev;
          setRevision((r) => r + 1);
          return d;
        });
      })
      .catch(() => {
        /* the document may just have been deleted */
      });
  }, [version, id]);

  if (!doc || doc.id !== id) return <p className="muted pad">Loading…</p>;

  const summary = [
    doc.kind.toUpperCase(),
    `${(doc.size_bytes / 1024).toFixed(1)} KB`,
    doc.page_count ? `${doc.page_count} page(s)` : null,
    doc.text_method ? `text: ${doc.text_method}` : null,
    doc.ocr_pages ? `OCR pages: ${doc.ocr_pages}` : null,
    doc.avg_ocr_confidence ? `OCR confidence ${doc.avg_ocr_confidence}` : null,
    doc.processing_ms ? `${(doc.processing_ms / 1000).toFixed(1)}s` : null,
    `attempts: ${doc.attempts}`,
  ].filter(Boolean).join(" · ");

  async function reprocess() {
    await api(`/api/documents/${id}/reprocess`, { method: "POST" });
    toast("Re-queued");
    refresh();
  }
  async function remove() {
    if (!window.confirm("Delete this document and its data?")) return;
    await api(`/api/documents/${id}`, { method: "DELETE" });
    onDeleted();
    refresh();
  }
  async function showText() {
    setText(await api(`/api/documents/${id}/text?limit=5`));
  }

  return (
    <>
      <div className="card">
        <h3>{doc.filename} <span className={`badge ${statusClass(doc.status)}`}>{doc.status}</span></h3>
        <div className="muted">{summary}</div>
        {doc.status === "failed" ? (
          <>
            <ul className="issues"><li className="error">{doc.error_code}: {doc.error_message}</li></ul>
            <button className="btn small" onClick={reprocess}>Reprocess</button>
          </>
        ) : null}
        {doc.warnings.map((w) => <div key={w} className="badge warn">{w}</div>)}
        <div className="actions">
          <button
            className="btn small secondary"
            onClick={() => downloadFile(doc.id, doc.filename).catch((e: Error) => toast(e.message))}
          >
            Download original
          </button>
          <button className="btn small secondary" onClick={showText}>Show extracted text</button>
          <button className="btn small secondary" onClick={remove}>Delete</button>
        </div>
      </div>

      {doc.invoices.map((inv) => <InvoiceCard key={`${inv.id}:${revision}`} inv={inv} onChanged={load} />)}

      <div className="card">
        <h3>Processing history</h3>
        <table>
          <thead>
            <tr><th>Attempt</th><th>Stage</th><th>Status</th><th>Duration</th><th>Message</th></tr>
          </thead>
          <tbody>
            {doc.events.map((e, n) => (
              <tr key={n}>
                <td>{e.attempt}</td>
                <td>{e.stage}</td>
                <td><span className={`badge ${e.status === "ok" ? "ok" : "err"}`}>{e.status}</span></td>
                <td>{e.duration_ms != null ? `${e.duration_ms} ms` : ""}</td>
                <td>{e.message ?? ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {text ? (
        <div className="card">
          <h3>Extracted text (first {text.pages.length} of {text.page_count})</h3>
          {text.pages.map((p) => (
            <div key={p.page_no}>
              <div className="muted">
                Page {p.page_no}{p.label ? ` – ${p.label}` : ""} · {p.method}{p.confidence ? ` · confidence ${p.confidence}` : ""}
              </div>
              <pre className="text">{p.text}</pre>
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}
