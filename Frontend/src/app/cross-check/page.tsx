"use client";

import { useEffect, useState } from "react";
import { useApp } from "@/components/AppContext";
import { api } from "@/lib/api";
import type { ReconcileGroup } from "@/lib/types";

export default function CrossCheckPage() {
  const { version, toast } = useApp();
  const [groups, setGroups] = useState<ReconcileGroup[] | null>(null);
  const [showAll, setShowAll] = useState(false);

  useEffect(() => {
    api<{ groups: ReconcileGroup[] }>(`/api/reconcile?only_mismatches=${!showAll}`)
      .then((r) => setGroups(r.groups))
      .catch((e: Error) => toast(`Could not load cross-check: ${e.message}`));
  }, [showAll, version, toast]);

  return (
    <section>
      <p className="muted">
        The same invoice (supplier + number) found in more than one document, e.g. PDF vs Excel, with any field differences.
      </p>
      <label>
        <input id="allGroups" type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} /> Show matching groups too
      </label>
      <div id="reconcileList">
        {groups?.length === 0 ? <p className="muted">No cross-document matches found.</p> : null}
        {groups?.map((g, n) => (
          <div key={n} className="card">
            <h3>
              {g.supplier_name ?? "?"} · {g.invoice_number}{" "}
              <span className={`badge ${g.consistent ? "ok" : "err"}`}>{g.consistent ? "consistent" : "differences"}</span>
            </h3>
            <div className="muted">{g.records.map((r) => `${r.filename} (${r.kind})`).join("  ↔  ")}</div>
            {g.differences.length ? (
              <table>
                <thead><tr><th>Field</th><th>Values per document</th></tr></thead>
                <tbody>
                  {g.differences.map((df) => (
                    <tr key={df.field}>
                      <td>{df.field}</td>
                      <td>{df.values.map((v) => `${v.filename}: ${v.value}`).join("   |   ")}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}
