"""Document processing pipeline: load -> segment -> extract -> validate -> index -> persist."""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import or_, select, update

from .db import Document, Invoice, PageText, ProcessingEvent, ValidationIssue, utcnow
from .extraction import enrich_with_llm, extract_text
from .extraction.tabular import extract_sheets
from .loaders import load_document
from .loaders.base import ProcessingError
from .schemas import REQUIRED_FIELDS, ExtractionResult, FieldMeta, InvoiceData
from .search import embedding_text, invoice_search_text, search_text_body, to_blob
from .segment import segment_pages
from .services import Services
from .utils import normalize_name, normalize_number
from .validation import needs_review, validate

log = logging.getLogger(__name__)


class NoTextError(ProcessingError):
    code = "no_text_extracted"


@dataclass
class Candidate:
    result: ExtractionResult
    text: str
    page_start: int | None = None
    page_end: int | None = None


@dataclass
class _Events:
    items: list[tuple[str, str, str | None, int]] = field(default_factory=list)

    @contextmanager
    def stage(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        except Exception as exc:
            self.items.append((name, "error", f"{type(exc).__name__}: {exc}"[:500], int((time.perf_counter() - t0) * 1000)))
            raise
        else:
            self.items.append((name, "ok", None, int((time.perf_counter() - t0) * 1000)))


# --------------------------------------------------------------------------- keys & shared recompute
def dedupe_key(data: InvoiceData) -> str | None:
    num = normalize_number(data.invoice_number)
    if not num:
        return None
    sup = normalize_name(data.supplier_name).replace(" ", "")
    if sup:
        return f"{sup}|{num}"
    return f"?|{num}|{data.total}"


def confidence_of(data: InvoiceData, fields_meta: dict[str, dict]) -> float:
    vals = [
        float(fields_meta.get(f, {}).get("confidence", 0.0)) if getattr(data, f) is not None else 0.0
        for f in REQUIRED_FIELDS
    ]
    return round(sum(vals) / len(vals), 3)


def set_columns(inv: Invoice, data: InvoiceData) -> None:
    inv.invoice_number = data.invoice_number
    inv.number_key = normalize_number(data.invoice_number) or None
    inv.supplier_name = data.supplier_name
    inv.supplier_key = normalize_name(data.supplier_name) or None
    inv.customer_name = data.customer_name
    inv.invoice_date = data.invoice_date
    inv.currency = data.currency
    inv.total = float(data.total) if data.total is not None else None
    inv.dedupe_key = dedupe_key(data)


def find_duplicates(session, inv: Invoice) -> list[str]:
    if not inv.dedupe_key:
        return []
    stmt = select(Invoice.id).where(Invoice.dedupe_key == inv.dedupe_key, Invoice.id != inv.id).limit(10)
    return list(session.scalars(stmt))


def refresh_invoice(session, svc: Services, inv: Invoice, *, filename: str = "", body: str | None = None,
                    reembed: bool = True) -> None:
    """Re-derive everything computed from `inv.data`: columns, issues, review flag, search text, embedding."""
    data = InvoiceData.model_validate(inv.data)
    ctx = inv.field_meta or {}
    fields = {k: FieldMeta(**v) for k, v in (ctx.get("fields") or {}).items()}
    set_columns(inv, data)
    issues = validate(
        data, fields, svc.settings,
        duplicates=find_duplicates(session, inv),
        ambiguous_date=bool(ctx.get("ambiguous_date")),
        flags=ctx.get("flags") or [],
        extraction_warnings=ctx.get("warnings") or [],
    )
    inv.issues = [ValidationIssue(code=i.code, severity=i.severity, field=i.field, message=i.message) for i in issues]
    inv.needs_review = needs_review(issues) and inv.status != "approved"
    inv.confidence = confidence_of(data, ctx.get("fields") or {})
    if reembed:
        if body is None:
            body = search_text_body(inv.search_text)
        inv.search_text = invoice_search_text(inv.data, body, filename)
        inv.embedding = to_blob(svc.embedder.embed([embedding_text(inv.data, body)])[0])
        inv.embedding_model = svc.embedder.name


def reindex_stale(svc: Services, batch_size: int = 64) -> int:
    """Re-embed invoices written by another embedding model. Vectors from different models are not
    comparable, so without this they would silently vanish from semantic search after a model change."""
    done = 0
    stale = or_(Invoice.embedding_model != svc.embedder.name, Invoice.embedding_model.is_(None))
    while True:
        with svc.session_factory() as s:
            rows = list(s.scalars(select(Invoice).where(stale).limit(batch_size)))
            if not rows:
                return done
            vectors = svc.embedder.embed([embedding_text(r.data, search_text_body(r.search_text)) for r in rows])
            for inv, vec in zip(rows, vectors):
                inv.embedding = to_blob(vec)
                inv.embedding_model = svc.embedder.name
            s.commit()
            done += len(rows)


# --------------------------------------------------------------------------- orchestration
def _progress_cb(svc: Services, doc_id: str):
    state = {"t": 0.0}

    def cb(done: int, total: int) -> None:
        now = time.monotonic()
        if done < total and now - state["t"] < 0.5:
            return
        state["t"] = now
        with svc.session_factory() as s:
            s.execute(update(Document).where(Document.id == doc_id).values(page_count=total, pages_done=done))
            s.commit()

    return cb


def _mark_failed(svc: Services, doc_id: str, attempt: int, code: str, message: str, events: _Events, started: float) -> None:
    with svc.session_factory() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            return
        doc.status = "failed"
        doc.error_code = code
        doc.error_message = message[:2000]
        doc.finished_at = utcnow()
        doc.processing_ms = int((time.perf_counter() - started) * 1000)
        for name, status, msg, ms in events.items:
            doc.events.append(ProcessingEvent(attempt=attempt, stage=name, status=status, message=msg, duration_ms=ms))
        s.commit()


def run_document(svc: Services, doc_id: str) -> None:
    """Process a document, retrying transient failures. Never raises."""
    S = svc.settings
    for attempt in range(1, S.max_attempts + 1):
        started = time.perf_counter()
        events = _Events()
        try:
            _process_once(svc, doc_id, attempt, events, started)
            return
        except ProcessingError as exc:
            if exc.retryable and attempt < S.max_attempts:
                log.warning("doc %s attempt %s transient failure: %s", doc_id, attempt, exc)
                time.sleep(S.retry_backoff_s * (2 ** (attempt - 1)))
                continue
            _mark_failed(svc, doc_id, attempt, exc.code, str(exc), events, started)
            return
        except Exception as exc:  # unexpected: retry, then give up with a generic code
            log.exception("doc %s attempt %s crashed", doc_id, attempt)
            if attempt < S.max_attempts:
                time.sleep(S.retry_backoff_s * (2 ** (attempt - 1)))
                continue
            _mark_failed(svc, doc_id, attempt, "internal_error", f"{type(exc).__name__}: {exc}", events, started)
            return


def _process_once(svc: Services, doc_id: str, attempt: int, events: _Events, started: float) -> None:
    S = svc.settings
    with svc.session_factory() as s:
        doc = s.get(Document, doc_id)
        if doc is None:
            return
        path = S.upload_dir / doc.stored_name
        filename = doc.filename
        sha = doc.sha256
        doc.status = "processing"
        doc.attempts = attempt
        doc.started_at = utcnow()
        doc.error_code = doc.error_message = None
        doc.pages_done = 0
        for p in list(doc.pages):
            s.delete(p)
        for inv in list(doc.invoices):
            s.delete(inv)
        s.commit()

    with events.stage("load"):
        loaded = load_document(path, filename, S, _progress_cb(svc, doc_id))
    total_chars = sum(len(p.text.strip()) for p in loaded.pages)
    if total_chars == 0:
        events.items.append(("load", "error", "no text could be extracted", 0))
        raise NoTextError("No text could be extracted (blank or unreadable document)")

    with events.stage("extract"):
        cands: list[Candidate] = []
        # LLM calls take seconds each; without a pool a 1000-invoice PDF would hold a worker for hours
        with ThreadPoolExecutor(max_workers=S.llm_workers if svc.llm else 1) as pool:
            if loaded.kind in ("pdf", "image"):
                segs = segment_pages(loaded.pages)
                results = pool.map(lambda seg: extract_text(seg.text, S, svc.llm), segs)
                cands.extend(Candidate(r, seg.text, min(seg.pages), max(seg.pages)) for r, seg in zip(results, segs))
            else:
                sheets = extract_sheets(loaded.sheets, dayfirst=S.date_dayfirst)
                results = pool.map(lambda sh: enrich_with_llm(sh[0], sh[1], S, svc.llm), sheets)
                cands.extend(Candidate(r, sh[1]) for r, sh in zip(results, sheets))
        if not cands:  # nothing recognisable: keep a placeholder so a human can fill it in
            full = "\n\n".join(p.text for p in loaded.pages)
            cands.append(Candidate(ExtractionResult(), full, 1, len(loaded.pages)))
            loaded.warnings.append("No invoice data recognised; created an empty record for manual entry")

    with events.stage("persist"):
        with svc.session_factory() as s:
            doc = s.get(Document, doc_id)
            for p in loaded.pages:
                s.add(PageText(document_id=doc_id, page_no=p.page_no, label=p.label, method=p.method,
                               confidence=p.confidence, text=p.text))
            vectors = svc.embedder.embed([embedding_text(c.result.data.model_dump(mode="json"), c.text) for c in cands])
            with svc.persist_lock:  # duplicate check + insert must be atomic across workers
                for n, c in enumerate(cands):
                    r = c.result
                    inv = Invoice(
                        document_id=doc_id, segment_index=n, page_start=c.page_start, page_end=c.page_end,
                        data=r.data.model_dump(mode="json"),
                        field_meta={
                            "fields": {k: v.model_dump() for k, v in r.meta.items()},
                            "ambiguous_date": r.ambiguous_date, "flags": r.flags, "warnings": r.warnings,
                        },
                        status="extracted",
                    )
                    s.add(inv)
                    s.flush()
                    refresh_invoice(s, svc, inv, filename=filename, body=c.text, reembed=False)
                    inv.search_text = invoice_search_text(inv.data, c.text, filename)
                    inv.embedding = to_blob(vectors[n])
                    inv.embedding_model = svc.embedder.name
                    s.flush()
                dup_files = s.scalar(
                    select(Document.id).where(Document.sha256 == sha, Document.id != doc_id, Document.status == "completed").limit(1)
                )
                warnings = list(loaded.warnings)
                if dup_files:
                    warnings.append(f"Identical file already processed (document {dup_files})")
                doc.warnings = warnings
                doc.kind = loaded.kind if loaded.kind != "xlsx" or doc.kind == "xlsx" else doc.kind
                doc.page_count = len(loaded.pages)
                doc.pages_done = len(loaded.pages)
                doc.ocr_pages = loaded.ocr_pages
                doc.text_method = loaded.text_method
                doc.avg_ocr_confidence = loaded.avg_ocr_confidence
                doc.status = "completed"
                doc.finished_at = utcnow()
                doc.processing_ms = int((time.perf_counter() - started) * 1000)
                for name, status, msg, ms in events.items:
                    doc.events.append(ProcessingEvent(attempt=attempt, stage=name, status=status, message=msg, duration_ms=ms))
                doc.events.append(ProcessingEvent(attempt=attempt, stage="persist", status="ok",
                                                  message=f"{len(cands)} invoice record(s)", duration_ms=None))
                s.commit()
