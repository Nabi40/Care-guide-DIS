"use client";

import { useState } from "react";
import { api } from "@/lib/api";
import type { Invoice, LineItem, ScalarField } from "@/lib/types";
import { useApp } from "./AppContext";

const SCALARS: [ScalarField, string, string][] = [
  ["invoice_number", "Invoice number", "text"],
  ["invoice_date", "Invoice date", "date"],
  ["supplier_name", "Supplier", "text"],
  ["customer_name", "Customer", "text"],
  ["subtotal", "Subtotal", "text"],
  ["tax", "Tax", "text"],
  ["total", "Total", "text"],
  ["currency", "Currency", "text"],
];
const ITEM_COLS: (keyof LineItem)[] = ["description", "quantity", "unit_price", "amount"];

type Row = Record<keyof LineItem, string> & { key: number };

const toRow = (li: Partial<LineItem>, key: number): Row => ({
  key,
  description: li.description ?? "",
  quantity: li.quantity ?? "",
  unit_price: li.unit_price ?? "",
  amount: li.amount ?? "",
});
const blankToNull = (v: string): string | null => (v.trim() === "" ? null : v.trim());

interface Props {
  inv: Invoice;
  onChanged: () => void;
}

export function InvoiceCard({ inv, onChanged }: Props) {
  const { toast, refresh } = useApp();
  const d = inv.data;
  const [fields, setFields] = useState(() =>
    Object.fromEntries(SCALARS.map(([k]) => [k, d[k] ?? ""])) as Record<ScalarField, string>,
  );
  const [rows, setRows] = useState<Row[]>(() => (d.line_items ?? []).map(toRow));
  const [nextKey, setNextKey] = useState(() => (d.line_items ?? []).length);

  const severity = (key: string) => {
    const hits = inv.issues.filter((i) => i.field === key);
    if (hits.some((i) => i.severity === "error")) return "has-error";
    return hits.length ? "has-warn" : "";
  };
  const editRow = (key: number, col: keyof LineItem, value: string) =>
    setRows((rs) => rs.map((r) => (r.key === key ? { ...r, [col]: value } : r)));

  async function save() {
    const patch: Record<string, unknown> = {};
    for (const [key] of SCALARS) {
      const value = fields[key].trim();
      if (value !== (d[key] ?? "")) patch[key] = blankToNull(value);
    }
    const items = rows.map((r) => Object.fromEntries(ITEM_COLS.map((c) => [c, blankToNull(r[c])])));
    const original = (d.line_items ?? []).map((l) => Object.fromEntries(ITEM_COLS.map((c) => [c, l[c] ?? null])));
    if (JSON.stringify(items) !== JSON.stringify(original)) patch.line_items = items;
    if (!Object.keys(patch).length) return toast("No changes to save");
    try {
      await api(`/api/invoices/${inv.id}`, { method: "PATCH", json: patch });
      toast("Saved and revalidated");
      onChanged();
      refresh();
    } catch (e) {
      toast(`Could not save: ${(e as Error).message}`);
    }
  }

  async function approve() {
    const errors = inv.issues.filter((i) => i.severity === "error").length;
    let note: string | null = null;
    if (errors) {
      note = window.prompt(`${errors} validation error(s) remain. Enter a reason to approve anyway:`);
      if (!note) return;
    }
    try {
      await api(`/api/invoices/${inv.id}/approve`, { method: "POST", json: { note } });
      toast("Approved");
      onChanged();
      refresh();
    } catch (e) {
      toast((e as Error).message);
    }
  }

  return (
    <div className="card">
      <h3>
        Invoice {d.invoice_number || "(no number)"}{" "}
        <span className={`badge ${inv.needs_review ? "warn" : "ok"}`}>{inv.needs_review ? "needs review" : inv.status}</span>{" "}
        <span className="muted">
          confidence {(inv.confidence * 100).toFixed(0)}%
          {inv.page_start ? ` · pages ${inv.page_start}–${inv.page_end}` : ""}
        </span>
      </h3>

      <div className="grid">
        {SCALARS.map(([key, label, type]) => {
          const meta = inv.field_meta[key];
          const id = `f-${inv.id}-${key}`;
          return (
            <div key={key} className={`field ${severity(key)}`}>
              <label htmlFor={id}>{label}</label>
              <input
                id={id}
                type={type}
                autoComplete="off"
                value={fields[key]}
                onChange={(e) => setFields((f) => ({ ...f, [key]: e.target.value }))}
              />
              <small>
                {meta
                  ? `${meta.source} · ${(meta.confidence * 100).toFixed(0)}%${meta.evidence ? ` · “${meta.evidence}”` : ""}`
                  : "not found"}
              </small>
            </div>
          );
        })}
      </div>

      <h3 className="sub">Line items</h3>
      <table>
        <thead>
          <tr><th>Description</th><th>Qty</th><th>Unit price</th><th>Amount</th><th /></tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key}>
              {ITEM_COLS.map((c) => (
                <td key={c} className={c === "description" ? "" : "num"}>
                  <input aria-label={c} value={r[c]} onChange={(e) => editRow(r.key, c, e.target.value)} />
                </td>
              ))}
              <td>
                <button
                  className="link"
                  type="button"
                  aria-label="Remove line"
                  onClick={() => setRows((rs) => rs.filter((x) => x.key !== r.key))}
                >
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button
        className="btn small secondary"
        type="button"
        onClick={() => { setRows((rs) => [...rs, toRow({}, nextKey)]); setNextKey((k) => k + 1); }}
      >
        + line
      </button>

      {inv.issues.length ? (
        <ul className="issues">
          {inv.issues.map((i, n) => (
            <li key={n} className={i.severity}>
              <b>{i.code}</b>{i.field ? ` (${i.field})` : ""}: {i.message}
            </li>
          ))}
        </ul>
      ) : (
        <p className="badge ok">No validation issues</p>
      )}

      <div className="actions">
        <button className="btn" type="button" onClick={save}>Save corrections</button>
        <button className="btn secondary" type="button" onClick={approve}>Approve</button>
      </div>

      {inv.corrections.length ? (
        <details>
          <summary className="muted">{inv.corrections.length} manual correction(s)</summary>
          <table>
            <tbody>
              {inv.corrections.map((c, n) => (
                <tr key={n}><td>{c.field}</td><td>{c.old ?? "∅"}</td><td>→</td><td>{c.new ?? "∅"}</td></tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : null}
    </div>
  );
}
