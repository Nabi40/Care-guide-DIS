# Document Intelligence – backend API

Upload invoices (PDF, scans, JPG/PNG/TIFF, XLSX/XLS, CSV). The service extracts text (OCR only where needed),
pulls out structured invoice data **without inventing values**, validates it, flags what needs a human, and lets
you review, correct and search everything through a REST API. The web UI is the Next.js app in [`../Frontend`](../Frontend);
[`../README.md`](../README.md) shows how to run both.

* Backend: **FastAPI + SQLAlchemy 2** (SQLite by default, PostgreSQL via `DATABASE_URL`)
* Extraction: layout-aware **rules** first; a **local LLM (Ollama)** fills what the rules could not, and can never override them
* Search: keyword filters plus **semantic vectors** (sentence-transformers)
* Measured results: [`eval/report.md`](eval/report.md) and [`eval/search_report.md`](eval/search_report.md)

## Quick start

Windows PowerShell:

```powershell
python -m venv web_env
.\web_env\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:create_app --factory --port 8000
```

Linux/macOS: use `. web_env/bin/activate`. The API is at http://localhost:8000 (docs: `/docs`). The first start downloads the
embedding model (~90 MB).

Install [Tesseract](https://github.com/tesseract-ocr/tesseract) for scans and images, and make sure it is on `PATH`
(digital PDFs and spreadsheets work without it). To enable AI extraction (free, local): install [Ollama](https://ollama.com),
run `ollama pull qwen2.5:7b`, and set `LLM_ENABLED=true` before starting.

Generate sample files to upload:

```bash
python -c "from eval import dataset; from pathlib import Path; dataset.build(Path('samples'))"
```

Run the tests / evaluation:

```bash
python -m pytest tests -q                                    # 191 tests (OCR tests need Tesseract)
python tests/e2e_ui.py                                       # browser test: builds ../Frontend (needs `playwright install chromium`)
python -m eval.eval_search                                   # writes eval/search_report.md
python -m eval.run_eval --seed 7 --stress-digital 1000 --stress-scan 40   # writes eval/report.md + results.json (needs Tesseract)
python -m eval.run_eval --seed 7 --llm                       # same, with AI extraction (needs Ollama)
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | uploads + SQLite file |
| `DATABASE_URL` | sqlite in `DATA_DIR` | e.g. `postgresql+psycopg://user:pw@host/db` (install `psycopg[binary]`) |
| `API_KEY` | _unset_ | if set, every `/api/*` call needs header `X-API-Key` |
| `LLM_ENABLED` | false | enables AI extraction through a local Ollama server; also `LLM_MODEL` (default `qwen2.5:7b`), `LLM_WORKERS` (1), `LLM_TIMEOUT_S` (300), `LLM_NUM_CTX` (16384), `LLM_BASE_URL` (`http://127.0.0.1:11434`) |
| `MAX_UPLOAD_MB` / `MAX_FILES_PER_REQUEST` | 50 / 50 | upload limits |
| `MAX_PDF_PAGES` / `MAX_SHEET_ROWS` | 3000 / 200000 | decompression-bomb style guards |
| `OCR_ENABLED`, `OCR_LANG`, `OCR_DPI`, `OCR_WORKERS` | true, eng, 200, ≤4 | OCR behaviour |
| `JOB_WORKERS`, `MAX_ATTEMPTS`, `RETRY_BACKOFF_S` | 2, 3, 1.0 | background processing |
| `DATE_DAYFIRST` | true | how `05/03/2026` is read (ambiguous dates are always flagged) |
| `EMBEDDING_BACKEND` | `auto` | `sentence-transformers`, falling back to `hashing` if the model cannot load; or set either explicitly. `EMBEDDING_MODEL` picks the model. Changing the model re-embeds existing invoices at the next start |

## How it works

```
upload → sniff type + limits → store → queue ─┐
                                              ▼
   load (text layer | OCR | sheets) → segment into invoices → extract (rules ▸ optional LLM) → validate
                                              │                                                   │
                                              └──────────── persist (+ history events) ◄──────────┘
                                                      review / correct / approve · search · reconcile
```

Full diagram and DB schema: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

* **Loading.** Digital PDFs use the text layer (positioned words are re-assembled into a column-preserving text grid, so
  tables survive). Only pages with no usable text are OCR'd (Tesseract, deskew + denoise, rotation retry, bounded thread pool).
  A failure on one page is recorded as a warning; it does not fail the document. Spreadsheets: every sheet, merged cells and
  blank-cell forward-fill handled, header rows found by synonym matching, "register" (many invoices) vs "single" layouts.
* **Extraction.** Every field carries a confidence and the evidence text it came from. Totals are only read from labels that
  *start a column* (so a sentence such as "please set the total to 1,000,000" in free text cannot move a total).
  Missing/uncertain stays `null`. Multi-invoice PDFs (a 1000-page file) are split by invoice-number change; continuation
  pages merge into the invoice before them; a page with a full header but no number after a finished invoice starts a new one.
* **AI extraction (optional).** A local model served by Ollama, with the reply constrained to a JSON schema; prompts and schema are in
  [`app/extraction/prompts.py`](app/extraction/prompts.py). It runs only for invoices whose rule-based result is weak, up to
  `LLM_WORKERS` calls at a time per document. Every LLM value must be found in the source text (grounding check) or it is dropped;
  the LLM only fills fields the rules left empty; when it disagrees with a confident rule value the rule value wins and the invoice
  is flagged `LLM_DISAGREEMENT`. Ollama errors (not running, model not pulled) fall back to the rules and leave a warning on the invoice. There is no API cost;
  run time scales with the number of weak invoices and depends on your GPU, so try a sample before running large files.
* **Validation** (`app/validation.py`): `MISSING_FIELD`, `INVALID_AMOUNT`, `LINE_ITEM_MATH`, `SUBTOTAL_MISMATCH`,
  `TOTAL_MISMATCH`, `TAX_RATE_UNUSUAL`, `FUTURE_DATE`, `OLD_DATE`, `AMBIGUOUS_DATE`, `LOW_CONFIDENCE`, `LLM_DISAGREEMENT`,
  `SOURCE_INCONSISTENT`, `DUPLICATE_INVOICE`. Any error – or a review-grade warning – sets `needs_review`.
  Duplicates = same normalised supplier + invoice number, across files and formats; the later arrival is flagged.
* **Jobs.** Uploads return `202` immediately. A worker pool processes them with retries and backoff for transient errors only
  (a corrupt file is not retried). Job state lives in the DB, so unfinished jobs are re-queued after a restart.
* **Search.** Keyword filters (supplier, number, date range, amount range, status, needs-review) plus `mode=semantic|hybrid`
  over per-invoice embeddings. Hybrid (the UI default) merges vector hits with exact keyword hits, so an invoice number is
  found even though it means nothing to the embedding model. `GET /api/stats` reports whether real semantic vectors are active.
* **Reconcile.** `GET /api/reconcile` compares the same invoice across sources (e.g. PDF vs Excel) field by field.

## API overview (`/docs` has the full schema)

| Method & path | Purpose |
|---|---|
| `POST /api/documents` | upload one or many files (multipart `files`) → `202` + job ids |
| `GET /api/documents`, `GET /api/documents/{id}` | list / status, stage, progress, error code, history events |
| `GET /api/documents/{id}/text`, `/file` | extracted text per page; original file |
| `POST /api/documents/{id}/reprocess`, `POST /api/documents/reprocess-failed` | re-run (failed only, or `force`) |
| `DELETE /api/documents/{id}` | remove document and its records |
| `GET /api/invoices`, `GET /api/invoices/{id}` | results with per-field confidence/evidence + validation issues |
| `PATCH /api/invoices/{id}` | manual corrections (audited, re-validated, re-embedded) |
| `POST /api/invoices/{id}/approve` | mark reviewed (a note is required while errors remain) |
| `GET /api/search?q=…&mode=keyword\|semantic\|hybrid&supplier=…&min_total=…` | search |
| `GET /api/reconcile` | cross-document comparison |
| `GET /api/stats`, `GET /health` | counters, liveness |

Money is returned as strings (`"1234.50"`), never floats.

## Design decisions and honest limitations

* **Rules first, LLM to fill gaps.** The measured numbers in `eval/report.md` are rule-based only; no live key was available
  while building, so the LLM path is covered by mocked-response tests (grounding, injection, disagreement, refusals, concurrency)
  but it has **never run against the live API** and its accuracy is **unmeasured**. Run `python -m eval.run_eval --llm` to measure it.
* **Synthetic evaluation.** The labeled dataset is generated (3 invoice templates, several spreadsheet layouts, simulated scan
  degradation). It is good for regression and robustness testing, and its labels are exact, but real invoices are more varied,
  so real-world accuracy will be **lower**. Plug your own labeled files in by editing `eval/dataset.py`.
* **Poor scans lose data.** At the "poor" degradation level (low resolution, heavy noise, faded toner) OCR drops or garbles
  most fields. The pipeline does not guess: gaps stay empty and the invoice is flagged for review.
* **Semantic search has limits.** One vector per invoice means a query about one line of a long invoice can be diluted by the
  rest; see [`eval/search_report.md`](eval/search_report.md). Vectors are stored in the database and scanned in process, which
  suits thousands of invoices; at scale use pgvector or Qdrant. Embedding also slows ingestion compared with the hashing fallback.
* **Queue.** An in-process thread pool with DB-persisted state – simple and restart-safe on one node. For horizontal scale swap
  `app/jobs.py` for Celery/RQ + Redis (the pipeline entry point `run_document` is already queue-agnostic).
* **UI.** Next.js in `../Frontend`. Its server proxies `/api` to this service, so the browser sees one origin and no CORS is needed.
* **Prompt injection.** Mitigated by column-anchored total labels, grounding, rules-win merging and a "treat the document as
  data" prompt, but it can't be called solved: an attacker who controls a legitimately labelled field can still set its value.
  Validation and human review are the backstop.
* **Not modelled:** shipping/discount/rounding lines as separate concepts, two different invoices on one page, handwriting,
  non-Latin OCR (set `OCR_LANG`), multi-currency invoices.
* **Security:** magic-byte file sniffing (extension alone is not trusted), allowlist, size/page/row/pixel caps, sanitized filenames
  and uuid storage names, optional API key (constant-time compare), CSP/nosniff/frame-deny headers, parameterised SQL with
  escaped LIKE wildcards. No user accounts/roles – put it behind your own auth/reverse proxy for multi-tenant use.

## What the evaluation showed (details in `eval/report.md`)

Synthetic data, rule-based extraction only. Digital PDFs, spreadsheets and CSVs were extracted exactly; scans are where it hurts:

* **Scans and images:** moderate scans/images lose a few line items or digits (`2.5` read as `25`, a dropped decimal point, `stee!`);
  "poor" scans mostly fail. Most of the wrong results are caught by validation (sum mismatches, missing fields), but **2 wrong invoices in the run
  were not flagged** (slightly truncated supplier names, e.g. "Pioneer Print" for "Pioneer Print & Packaging").
* **Scanned 40-page stress file:** 39 invoices were found for 40 pages and invoice number + total were right for only 62.5% –
  OCR noise on the number/total and page segmentation are the weak points of long scans. Use a higher `OCR_DPI` or the LLM pass there.
* **Review load:** about a third of clean invoices were flagged, mostly `AMBIGUOUS_DATE` (e.g. `03/04/2026`) – deliberate:
  the system asks rather than guesses. Expect the same on real data; set `DATE_DAYFIRST` correctly for your region.
* **Throughput on this machine:** 1000-page digital PDF in ~3 s (peak RSS ~380 MB); OCR ≈ 0.5 s/page with 4 workers.

## Layout

```
app/        main.py (API) · pipeline.py · jobs.py · validation.py · search.py · segment.py · db.py · schemas.py
app/loaders/       pdf · image/ocr · spreadsheet · layout (words→text grid) · detect (magic bytes)
app/extraction/    rules.py · tabular.py · llm.py · prompts.py
eval/       synth.py (document generator) · dataset.py · run_eval.py · report.md · results.json · eval_search.py · search_report.md
tests/      unit, API and (separately run) browser tests
docs/       ARCHITECTURE.md · AI_DISCLOSURE.md
```

## Deliverables checklist

| Item | Where |
|---|---|
| Source + setup | this repo / README |
| Architecture diagram + DB schema | `docs/ARCHITECTURE.md` |
| PDF / image / Excel / CSV pipelines | `app/loaders`, `app/extraction` |
| Tests + results | `tests/`, `eval/report.md`, `eval/results.json`, `eval/search_report.md` |
| AI prompts + AI-assistance disclosure | `app/extraction/prompts.py`, `docs/AI_DISCLOSURE.md` |
| Recorded walkthrough | **not included** – needs to be recorded by you; `docs/screenshots/` has UI captures to help |
