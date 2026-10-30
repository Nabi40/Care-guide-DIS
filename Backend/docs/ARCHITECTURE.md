# Architecture

## Components

```mermaid
flowchart LR
  UI[Web UI<br/>Next.js, proxies /api] -->|REST /api| API[FastAPI app<br/>main.py]
  Client[API clients] --> API
  API -->|store file + row<br/>status=queued| DB[(SQL DB<br/>SQLite / Postgres)]
  API --> FS[(Upload dir)]
  API -->|submit| Q[JobQueue<br/>thread pool, jobs.py]
  Q --> P[pipeline.run_document<br/>retry + backoff]
  P --> L[Loaders]
  L -->|digital PDF| PDF[PDFium text layer<br/>words → layout grid]
  L -->|scan / image| OCR[Tesseract<br/>deskew · denoise · rotate-retry]
  L -->|xlsx/xls/csv| SH[Sheet reader<br/>all sheets, merged cells]
  P --> SEG[segment.py<br/>split into invoices]
  SEG --> X[Extraction]
  X --> R[rules.py / tabular.py<br/>value + confidence + evidence]
  X -.optional.-> LLM[llm.py<br/>grounded, fills gaps only]
  X --> V[validation.py]
  V --> E[search.py<br/>embedding]
  E --> DB
  P -->|events| DB
  API -->|search · reconcile| DB
```

### Document lifecycle

`queued → processing (load → extract → persist) → completed | failed`. Each stage writes a `processing_events` row (stage, attempt, status, duration), so the
UI can show progress and history. Transient errors (OCR timeout, locked DB, LLM 5xx) retry with exponential backoff up to
`MAX_ATTEMPTS`; permanent errors (`corrupted_file`, `encrypted_file`, `unsupported_type`, `limit_exceeded`, `no_text_extracted`,
`ocr_unavailable`) fail immediately with a typed `error_code`. On startup, documents left in `queued/processing` are re-queued.

### Why these choices

| Concern | Choice | Reason / scale path |
|---|---|---|
| Large PDFs | page-at-a-time loading, OCR only for text-less pages, per-page failure tolerance, bounded OCR batches | a 1000-page digital PDF never needs OCR and never holds all page images in memory |
| Money | `Decimal` end to end, strings in JSON; floats only in the indexed `total` column used for range filters | avoids float drift in validation |
| Duplicates | `dedupe_key = normalized supplier + normalized invoice number`, checked under a lock at persist time | concurrent uploads of the same invoice cannot both pass |
| Search | SQL filters + cosine over stored float32 vectors | swap to pgvector/Qdrant when row counts make the numpy scan slow |
| Queue | in-process pool, state in DB | single node; Celery/RQ + Redis for many nodes (`run_document` is queue-agnostic) |

## Database schema

```mermaid
erDiagram
  documents ||--o{ page_texts : has
  documents ||--o{ invoices : yields
  documents ||--o{ processing_events : logs
  invoices  ||--o{ validation_issues : has
  invoices  ||--o{ corrections : audited_by

  documents {
    string id PK
    string filename
    string stored_name
    string kind "pdf|image|xlsx|xls|csv"
    int size_bytes
    string sha256
    string status "queued|processing|completed|failed"
    string error_code
    text error_message
    int attempts
    int page_count
    int pages_done
    int ocr_pages
    string text_method "digital|ocr|mixed|sheet"
    float avg_ocr_confidence
    json warnings
    int processing_ms
    datetime created_at
    datetime started_at
    datetime finished_at
  }
  page_texts {
    int id PK
    string document_id FK
    int page_no
    string label "sheet name"
    string method
    float confidence
    text text
  }
  invoices {
    string id PK
    string document_id FK
    int segment_index
    int page_start
    int page_end
    json data "full structured result incl. line_items"
    json field_meta "confidence + evidence per field, flags"
    string invoice_number
    string supplier_name
    string customer_name
    date invoice_date
    string currency
    float total
    string dedupe_key
    string status "extracted|corrected|approved"
    bool needs_review
    float confidence
    text review_note
    text search_text
    blob embedding
  }
  validation_issues {
    int id PK
    string invoice_id FK
    string code
    string severity "error|warning"
    string field
    text message
  }
  corrections {
    int id PK
    string invoice_id FK
    string field
    text old_value
    text new_value
    datetime created_at
  }
  processing_events {
    int id PK
    string document_id FK
    int attempt
    string stage
    string status "ok|error|warning"
    text message
    int duration_ms
    datetime created_at
  }
```

Indexes: `documents(sha256, status, created_at)`, `invoices(invoice_number, number_key, supplier_key+invoice_date, invoice_date,
currency, total, dedupe_key, needs_review)`, `validation_issues(code)`. Deleting a document cascades to everything below it.
(Column lists above are abridged to the load-bearing ones; `app/db.py` is authoritative.)

## Extraction and validation flow for one invoice

1. `rules.extract(text)` → candidate per field with `confidence` and `evidence`.
2. Optional `llm_extract(text)` → only values literally present in the text survive; they fill empty fields; a conflict with a
   confident rule value keeps the rule value and adds `LLM_DISAGREEMENT`.
3. `validation.validate(...)` → issues. Tolerances: totals ±0.02, line items ±0.01.
4. `needs_review = any error or review-grade warning` (`LOW_CONFIDENCE`, `AMBIGUOUS_DATE`, `LLM_DISAGREEMENT`, `SOURCE_INCONSISTENT`).
5. Manual correction (`PATCH`) records each changed field in `corrections`, marks that field `source=manual, confidence=1.0`, and
   re-runs validation and embedding. Approval with outstanding errors requires a note.
