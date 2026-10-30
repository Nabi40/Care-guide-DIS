"use client";

import { useEffect, useState } from "react";
import { useApp } from "@/components/AppContext";
import { InvoiceRow } from "@/components/InvoiceRow";
import { api } from "@/lib/api";
import type { Invoice } from "@/lib/types";

export default function ReviewPage() {
  const { version, toast } = useApp();
  const [items, setItems] = useState<Invoice[] | null>(null);

  useEffect(() => {
    api<{ items: Invoice[] }>("/api/invoices?needs_review=true&limit=100")
      .then((r) => setItems(r.items))
      .catch((e: Error) => toast(`Could not load the review queue: ${e.message}`));
  }, [version, toast]);

  return (
    <section>
      <p className="muted">Invoices flagged for manual review: validation errors, low confidence or ambiguous values.</p>
      <div id="reviewList">
        {items?.length === 0 ? <p className="badge ok">Nothing needs review</p> : null}
        {items?.map((inv) => <InvoiceRow key={inv.id} inv={inv} />)}
      </div>
    </section>
  );
}
