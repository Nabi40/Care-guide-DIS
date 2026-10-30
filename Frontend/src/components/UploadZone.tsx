"use client";

import { useRef, useState } from "react";
import { api } from "@/lib/api";
import { useApp } from "./AppContext";

interface UploadResult {
  accepted: { id: string }[];
  rejected: { filename: string; reason: string }[];
}

export function UploadZone() {
  const { toast, refresh } = useApp();
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);

  async function upload(files: File[]) {
    if (!files.length) return;
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    try {
      const r = await api<UploadResult>("/api/documents", { method: "POST", body: form });
      const rejected = r.rejected.length ? ` Rejected: ${r.rejected.map((x) => `${x.filename} (${x.reason})`).join(", ")}` : "";
      toast(`Accepted ${r.accepted.length} file(s).${rejected}`);
    } catch (e) {
      toast(`Upload failed: ${(e as Error).message}`);
    }
    refresh();
  }

  return (
    <div
      id="drop"
      className={over ? "drop over" : "drop"}
      tabIndex={0}
      role="button"
      aria-label="Upload documents"
      onClick={() => input.current?.click()}
      onKeyDown={(e) => e.key === "Enter" && input.current?.click()}
      onDragEnter={(e) => { e.preventDefault(); setOver(true); }}
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={(e) => { e.preventDefault(); setOver(false); }}
      onDrop={(e) => { e.preventDefault(); setOver(false); upload([...e.dataTransfer.files]); }}
    >
      <p><strong>Drop files here</strong> or <span className="link">browse</span></p>
      <p className="muted">PDF, JPG, PNG, TIFF, XLSX, XLS, CSV</p>
      <input
        ref={input}
        id="file"
        type="file"
        multiple
        hidden
        accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff,.xlsx,.xls,.csv"
        onClick={(e) => e.stopPropagation()}
        onChange={(e) => { upload([...(e.target.files ?? [])]); e.target.value = ""; }}
      />
    </div>
  );
}
