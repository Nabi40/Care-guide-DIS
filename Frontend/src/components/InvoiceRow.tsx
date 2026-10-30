"use client";

import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import type { Invoice } from "@/lib/types";

export function InvoiceRow({ inv, extra, children }: { inv: Invoice; extra?: ReactNode; children?: ReactNode }) {
  const router = useRouter();
  const d = inv.data;
  const open = () => router.push(`/?doc=${inv.document_id}`);
  return (
    <div className="card result" role="link" tabIndex={0} onClick={open} onKeyDown={(e) => e.key === "Enter" && open()}>
      <div>
        <b>{d.supplier_name || "(unknown supplier)"}</b>
        {` · ${d.invoice_number || "no number"} · ${d.invoice_date || "no date"} · `}
        <b>{d.total ?? "?"} {d.currency ?? ""}</b>
      </div>
      <div>
        <span className={`badge ${inv.needs_review ? "warn" : "ok"}`}>{inv.needs_review ? "review" : inv.status}</span>
        {extra ? " " : ""}
        {extra}
      </div>
      <div className="snip">
        {inv.filename}
        {inv.issues.length ? ` — ${inv.issues.slice(0, 3).map((i) => i.code).join(", ")}` : ""}
      </div>
      {children}
    </div>
  );
}
