"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";
import { DocumentList } from "@/components/DocumentList";
import { DocumentPanel } from "@/components/DocumentPanel";
import { UploadZone } from "@/components/UploadZone";

function Documents() {
  const router = useRouter();
  const selected = useSearchParams().get("doc");
  return (
    <div className="split">
      <div className="left">
        <UploadZone />
        <DocumentList selected={selected} onSelect={(id) => router.push(`/?doc=${id}`, { scroll: false })} />
      </div>
      <div className="right" id="detail">
        {selected ? (
          <DocumentPanel id={selected} onDeleted={() => router.push("/")} />
        ) : (
          <p className="muted pad">Select a document to inspect its extracted data.</p>
        )}
      </div>
    </div>
  );
}

export default function DocumentsPage() {
  return (
    <Suspense>
      <Documents />
    </Suspense>
  );
}
