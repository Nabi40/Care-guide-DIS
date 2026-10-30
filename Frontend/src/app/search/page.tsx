"use client";

import { useState, type FormEvent } from "react";
import { useApp } from "@/components/AppContext";
import { InvoiceRow } from "@/components/InvoiceRow";
import { api } from "@/lib/api";
import type { SearchResult } from "@/lib/types";

export default function SearchPage() {
  const { stats, toast } = useApp();
  const [result, setResult] = useState<SearchResult | null>(null);

  async function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const params = new URLSearchParams();
    for (const [k, v] of new FormData(e.currentTarget).entries()) {
      if (String(v).trim() !== "") params.set(k, String(v));
    }
    try {
      setResult(await api<SearchResult>(`/api/search?${params}`));
    } catch (err) {
      toast(`Search failed: ${(err as Error).message}`);
    }
  }

  return (
    <section>
      <form id="searchForm" className="searchform" onSubmit={submit}>
        <input name="q" type="search" placeholder="Search by meaning or exact text: “office cleaning”, INV-1042…" required className="grow" aria-label="Search" />
        <select name="mode" aria-label="Search mode" defaultValue="hybrid">
          <option value="hybrid">Hybrid</option>
          <option value="keyword">Keyword</option>
          <option value="semantic">Semantic</option>
        </select>
        <button className="btn" type="submit">Search</button>
        <details className="filters">
          <summary>Filters</summary>
          <label>Supplier <input name="supplier" /></label>
          <label>Customer <input name="customer" /></label>
          <label>Currency <input name="currency" maxLength={3} size={4} /></label>
          <label>From <input name="date_from" type="date" /></label>
          <label>To <input name="date_to" type="date" /></label>
          <label>Min total <input name="min_total" type="number" step="0.01" /></label>
          <label>Max total <input name="max_total" type="number" step="0.01" /></label>
          <label><input name="needs_review" type="checkbox" value="true" /> Needs review only</label>
        </details>
      </form>
      {stats && !stats.semantic_embeddings ? (
        <p className="badge warn">Semantic model unavailable: results match similar spelling, not meaning.</p>
      ) : null}
      {result ? (
        <div id="searchInfo" className="muted">
          {result.count} result(s) · {result.mode} · embeddings: {result.embedding_model}
        </div>
      ) : null}
      <div id="searchResults">
        {result?.items.map((inv) => (
          <InvoiceRow key={inv.id} inv={inv} extra={<span className="badge info">score {inv.score}</span>}>
            <div className="snip">…{inv.snippet}…</div>
          </InvoiceRow>
        ))}
      </div>
    </section>
  );
}
