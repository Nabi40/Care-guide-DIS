export type DocStatus = "queued" | "processing" | "completed" | "failed";

export interface Stats {
  documents: Partial<Record<DocStatus, number>>;
  documents_total: number;
  success_rate: number | null;
  invoices_total: number;
  needs_review: number;
  avg_processing_ms: number | null;
  llm_enabled: boolean;
  llm_model: string | null;
  embedding_model: string;
  semantic_embeddings: boolean;
}

export interface DocumentSummary {
  id: string;
  filename: string;
  kind: string;
  size_bytes: number;
  status: DocStatus;
  error_code: string | null;
  error_message: string | null;
  attempts: number;
  page_count: number;
  progress: number;
  ocr_pages: number;
  text_method: string | null;
  avg_ocr_confidence: number | null;
  warnings: string[];
  processing_ms: number | null;
  invoice_count: number;
  review_count: number;
}

export interface LineItem {
  description: string | null;
  quantity: string | null;
  unit_price: string | null;
  amount: string | null;
}

export interface InvoiceData {
  invoice_number: string | null;
  invoice_date: string | null;
  supplier_name: string | null;
  customer_name: string | null;
  subtotal: string | null;
  tax: string | null;
  total: string | null;
  currency: string | null;
  line_items: LineItem[];
}

export type ScalarField = Exclude<keyof InvoiceData, "line_items">;

export interface Issue {
  code: string;
  severity: "error" | "warning";
  field: string | null;
  message: string;
}

export interface Invoice {
  id: string;
  document_id: string;
  filename: string | null;
  page_start: number | null;
  page_end: number | null;
  data: InvoiceData;
  status: string;
  needs_review: boolean;
  confidence: number;
  field_meta: Record<string, { confidence: number; source: string; evidence: string | null }>;
  issues: Issue[];
  corrections: { field: string; old: string | null; new: string | null }[];
}

export interface HistoryEvent {
  attempt: number;
  stage: string;
  status: string;
  message: string | null;
  duration_ms: number | null;
}

export interface DocumentDetail extends DocumentSummary {
  events: HistoryEvent[];
  invoices: Invoice[];
}

export interface PageText {
  page_no: number;
  label: string | null;
  method: string;
  confidence: number | null;
  text: string;
}

export interface SearchHit extends Invoice {
  score: number;
  snippet: string;
}

export interface SearchResult {
  mode: string;
  embedding_model: string;
  count: number;
  items: SearchHit[];
}

export interface ReconcileGroup {
  invoice_number: string | null;
  supplier_name: string | null;
  consistent: boolean;
  differences: { field: string; values: { filename: string; value: unknown }[] }[];
  records: { filename: string; kind: string }[];
}
