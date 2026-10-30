"""FastAPI application: the REST API. The web UI is the separate Next.js app in ../Frontend.

Run:  uvicorn app.main:create_app --factory --port 8000
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import logging
import re
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated, Iterator

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy import case, distinct, func, select
from sqlalchemy.orm import Session

from . import search as search_mod
from .config import Settings
from .db import Base, Correction, Document, Invoice, make_engine, make_session_factory
from .jobs import JobQueue
from .loaders import ALLOWED_EXTENSIONS, detect_kind, sniff_head
from .loaders.base import UnsupportedFileError
from .pipeline import refresh_invoice, reindex_stale
from .schemas import ApproveBody, InvoiceData, InvoicePatch
from .services import Services, make_llm

log = logging.getLogger("docintel")
MEDIA ={".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".tif": "image/tiff", ".tiff": "image/tiff", ".csv": "text/csv",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         ".xls": "application/vnd.ms-excel"}


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.svc.session_factory() as s:
        yield s


DB = Annotated[Session, Depends(get_db)]


# --------------------------------------------------------------------------- serialisers
def doc_out(d: Document, invoice_count: int = 0, review_count: int = 0) -> dict:
    progress = 1.0 if d.status == "completed" else (d.pages_done / d.page_count if d.page_count else 0.0)
    return {
        "id": d.id, "filename": d.filename, "kind": d.kind, "size_bytes": d.size_bytes, "status": d.status,
        "error_code": d.error_code, "error_message": d.error_message, "attempts": d.attempts,
        "page_count": d.page_count, "pages_done": d.pages_done, "progress": round(min(progress, 1.0), 3),
        "ocr_pages": d.ocr_pages, "text_method": d.text_method, "avg_ocr_confidence": d.avg_ocr_confidence,
        "warnings": d.warnings or [], "processing_ms": d.processing_ms,
        "invoice_count": invoice_count, "review_count": review_count,
        "created_at": d.created_at, "finished_at": d.finished_at,
    }


def invoice_out(inv: Invoice, filename: str | None = None, detail: bool = False) -> dict:
    ctx = inv.field_meta or {}
    out = {
        "id": inv.id, "document_id": inv.document_id, "filename": filename,
        "segment_index": inv.segment_index, "page_start": inv.page_start, "page_end": inv.page_end,
        "data": inv.data, "status": inv.status, "needs_review": inv.needs_review,
        "confidence": inv.confidence,
        "issue_counts": {
            "error": sum(1 for i in inv.issues if i.severity == "error"),
            "warning": sum(1 for i in inv.issues if i.severity == "warning"),
        } if detail or "issues" in inv.__dict__ else None,
    }
    if detail:
        out.update({
            "field_meta": ctx.get("fields", {}),
            "issues": [{"code": i.code, "severity": i.severity, "field": i.field, "message": i.message} for i in inv.issues],
            "corrections": [{"field": c.field, "old": c.old_value, "new": c.new_value, "at": c.created_at}
                            for c in inv.corrections],
            "review_note": inv.review_note,
        })
    return out


def _safe_name(name: str | None) -> str:
    base = re.split(r"[\\/]", name or "upload")[-1]
    base = re.sub(r"[\x00-\x1f]", "", base).strip() or "upload"
    return base[:255]


# --------------------------------------------------------------------------- app factory
def create_app(
    settings: Settings | None = None,
    *,
    llm=None,
    embedder=None,
    start_workers: bool = True,
) -> FastAPI:
    S = settings or Settings.from_env()
    S.upload_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(S.db_url)
    Base.metadata.create_all(engine)
    svc = Services(
        settings=S,
        session_factory=make_session_factory(engine),
        embedder=embedder or search_mod.make_embedder(S.embedding_backend, S.embedding_model),
        llm=llm if llm is not None else make_llm(S),
    )
    queue = JobQueue(svc)

    def reindex_in_background() -> None:
        try:
            if n := reindex_stale(svc):
                log.info("re-embedded %s invoice(s) with %s", n, svc.embedder.name)
        except Exception:
            log.exception("re-embedding failed; affected invoices stay out of semantic search until the next start")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_workers:
            n = queue.recover()
            if n:
                log.info("re-queued %s unfinished document(s)", n)
            threading.Thread(target=reindex_in_background, name="reindex", daemon=True).start()
        yield
        queue.shutdown()
        engine.dispose()

    app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan)
    app.state.svc, app.state.queue = svc, queue

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith(("/docs", "/redoc", "/openapi.json")):
            resp.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return resp

    def require_key(request: Request) -> None:
        if S.api_key:
            given = request.headers.get("x-api-key", "")
            if not hmac.compare_digest(given.encode(), S.api_key.encode()):
                raise HTTPException(401, "Missing or invalid X-API-Key")

    api = APIRouter(prefix="/api", dependencies=[Depends(require_key)])

    # ----------------------------------------------------------------- health / stats
    @app.get("/")
    def root():
        return {"service": "Document Intelligence API", "docs": "/docs", "health": "/health"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @api.get("/stats")
    def stats(db: DB):
        by_status = dict(db.execute(select(Document.status, func.count()).group_by(Document.status)).all())
        done, failed = by_status.get("completed", 0), by_status.get("failed", 0)
        avg_ms = db.scalar(select(func.avg(Document.processing_ms)).where(Document.status == "completed"))
        return {
            "documents": by_status, "documents_total": sum(by_status.values()),
            "success_rate": round(done / (done + failed), 4) if (done + failed) else None,
            "invoices_total": db.scalar(select(func.count()).select_from(Invoice)) or 0,
            "needs_review": db.scalar(select(func.count()).select_from(Invoice).where(Invoice.needs_review.is_(True))) or 0,
            "avg_processing_ms": round(avg_ms) if avg_ms else None,
            "llm_enabled": svc.llm is not None, "llm_model": S.llm_model if svc.llm else None,
            "embedding_model": svc.embedder.name, "semantic_embeddings": not isinstance(svc.embedder, search_mod.HashingEmbedder),
        }

    # ----------------------------------------------------------------- documents
    @api.post("/documents", status_code=202)
    def upload(files: Annotated[list[UploadFile], File(description="One or more PDF/JPG/PNG/TIFF/XLSX/XLS/CSV files")], db: DB):
        if not files:
            raise HTTPException(400, "No files supplied")
        if len(files) > S.max_files_per_request:
            raise HTTPException(413, f"At most {S.max_files_per_request} files per request")
        accepted, rejected = [], []
        for f in files:
            name = _safe_name(f.filename)
            ext = Path(name).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                rejected.append({"filename": name, "reason": f"unsupported extension '{ext}'", "status": 415})
                continue
            doc_id = str(uuid.uuid4())
            stored = f"{doc_id}{ext}"
            dest = S.upload_dir / stored
            size, sha, too_big = 0, hashlib.sha256(), False
            with open(dest, "wb") as out:
                while chunk := f.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > S.max_upload_bytes:
                        too_big = True
                        break
                    sha.update(chunk)
                    out.write(chunk)
            if too_big:
                dest.unlink(missing_ok=True)
                rejected.append({"filename": name, "reason": f"larger than {S.max_upload_mb} MB", "status": 413})
                continue
            try:
                kind, _w = detect_kind(sniff_head(dest), name)
            except UnsupportedFileError as exc:
                dest.unlink(missing_ok=True)
                rejected.append({"filename": name, "reason": str(exc), "status": 415 if size else 400})
                continue
            doc = Document(id=doc_id, filename=name, stored_name=stored, kind=kind, size_bytes=size,
                           sha256=sha.hexdigest(), status="queued", warnings=[])
            db.add(doc)
            db.commit()
            queue.submit(doc_id)
            accepted.append({"id": doc_id, "filename": name, "status": "queued"})
        if not accepted:
            raise HTTPException(400, detail={"accepted": [], "rejected": rejected})
        return {"accepted": accepted, "rejected": rejected}

    @api.get("/documents")
    def list_documents(
        db: DB,
        status: str | None = Query(None, pattern="^(queued|processing|completed|failed)$"),
        q: str | None = Query(None, max_length=200),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        stmt = select(Document)
        if status:
            stmt = stmt.where(Document.status == status)
        if q:
            stmt = stmt.where(func.lower(Document.filename).like(f"%{search_mod._like_escape(q.casefold())}%", escape="\\"))
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        docs = list(db.scalars(stmt.order_by(Document.created_at.desc()).limit(limit).offset(offset)))
        ids = [d.id for d in docs]
        counts: dict[str, tuple[int, int]] = {}
        if ids:
            rows = db.execute(
                select(Invoice.document_id, func.count(), func.sum(case((Invoice.needs_review.is_(True), 1), else_=0)))
                .where(Invoice.document_id.in_(ids)).group_by(Invoice.document_id)).all()
            counts = {r[0]: (r[1], int(r[2] or 0)) for r in rows}
        return {"total": total, "items": [doc_out(d, *counts.get(d.id, (0, 0))) for d in docs]}

    def _doc_or_404(db: Session, doc_id: str) -> Document:
        d = db.get(Document, doc_id)
        if d is None:
            raise HTTPException(404, "Document not found")
        return d

    @api.get("/documents/{doc_id}")
    def get_document(doc_id: str, db: DB):
        d = _doc_or_404(db, doc_id)
        invs = list(d.invoices)
        out = doc_out(d, len(invs), sum(1 for i in invs if i.needs_review))
        out["events"] = [{"attempt": e.attempt, "stage": e.stage, "status": e.status, "message": e.message,
                          "duration_ms": e.duration_ms, "at": e.created_at} for e in d.events]
        out["invoices"] = [invoice_out(i, d.filename, detail=True) for i in sorted(invs, key=lambda i: i.segment_index)]
        return out

    @api.get("/documents/{doc_id}/text")
    def get_text(doc_id: str, db: DB, page: int | None = Query(None, ge=1), limit: int = Query(20, ge=1, le=200)):
        d = _doc_or_404(db, doc_id)
        pages = sorted(d.pages, key=lambda p: p.page_no)
        if page:
            pages = [p for p in pages if p.page_no == page]
        else:
            pages = pages[:limit]
        return {"document_id": doc_id, "page_count": d.page_count, "pages": [
            {"page_no": p.page_no, "label": p.label, "method": p.method, "confidence": p.confidence, "text": p.text}
            for p in pages]}

    @api.get("/documents/{doc_id}/file")
    def get_file(doc_id: str, db: DB):
        d = _doc_or_404(db, doc_id)
        path = S.upload_dir / d.stored_name
        if not path.is_file():
            raise HTTPException(404, "Stored file is missing")
        return FileResponse(path, filename=d.filename, media_type=MEDIA.get(path.suffix.lower(), "application/octet-stream"),
                            content_disposition_type="attachment")

    @api.post("/documents/{doc_id}/reprocess", status_code=202)
    def reprocess(doc_id: str, db: DB, force: bool = False):
        d = _doc_or_404(db, doc_id)
        if d.status in ("queued", "processing"):
            raise HTTPException(409, "Document is already being processed")
        if d.status == "completed" and not force:
            raise HTTPException(409, "Document already completed; pass force=true to reprocess (manual corrections are discarded)")
        d.status, d.error_code, d.error_message = "queued", None, None
        db.commit()
        queue.submit(d.id)
        return {"id": d.id, "status": "queued"}

    @api.post("/documents/reprocess-failed", status_code=202)
    def reprocess_failed(db: DB):
        failed = list(db.scalars(select(Document).where(Document.status == "failed")))
        for d in failed:
            d.status, d.error_code, d.error_message = "queued", None, None
        db.commit()
        for d in failed:
            queue.submit(d.id)
        return {"requeued": [d.id for d in failed]}

    @api.delete("/documents/{doc_id}", status_code=204)
    def delete_document(doc_id: str, db: DB):
        d = _doc_or_404(db, doc_id)
        if d.status == "processing":
            raise HTTPException(409, "Document is being processed")
        (S.upload_dir / d.stored_name).unlink(missing_ok=True)
        db.delete(d)
        db.commit()

    # ----------------------------------------------------------------- invoices
    def _inv_or_404(db: Session, inv_id: str) -> Invoice:
        inv = db.get(Invoice, inv_id)
        if inv is None:
            raise HTTPException(404, "Invoice not found")
        return inv

    @api.get("/invoices/{inv_id}")
    def get_invoice(inv_id: str, db: DB):
        inv = _inv_or_404(db, inv_id)
        return invoice_out(inv, inv.document.filename, detail=True)

    @api.patch("/invoices/{inv_id}")
    def patch_invoice(inv_id: str, body: InvoicePatch, db: DB):
        inv = _inv_or_404(db, inv_id)
        provided = body.model_fields_set
        if not provided:
            raise HTTPException(400, "No fields supplied")
        current = InvoiceData.model_validate(inv.data)
        merged = current.model_dump()
        for f in provided:
            v = getattr(body, f)
            merged[f] = [] if f == "line_items" and v is None else v
        try:
            updated = InvoiceData(**merged)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_context=False)))
        old, new = current.model_dump(mode="json"), updated.model_dump(mode="json")
        changed = [f for f in new if new[f] != old[f]]
        ctx = copy.deepcopy(inv.field_meta or {})
        fields = ctx.setdefault("fields", {})
        for f in changed:
            db.add(Correction(invoice_id=inv.id, field=f,
                              old_value=json.dumps(old[f]) if f == "line_items" else (None if old[f] is None else str(old[f])),
                              new_value=json.dumps(new[f]) if f == "line_items" else (None if new[f] is None else str(new[f]))))
            fields[f] = {"confidence": 1.0, "source": "manual", "evidence": None}
        if changed:
            ctx["flags"], ctx["warnings"] = [], []
            if "invoice_date" in changed:
                ctx["ambiguous_date"] = False
            inv.data = new
            inv.field_meta = ctx
            inv.status = "corrected"
            inv.review_note = None
        refresh_invoice(db, svc, inv, filename=inv.document.filename)
        db.commit()
        return invoice_out(inv, inv.document.filename, detail=True)

    @api.post("/invoices/{inv_id}/approve")
    def approve(inv_id: str, db: DB, body: ApproveBody | None = None):
        inv = _inv_or_404(db, inv_id)
        errors = [i for i in inv.issues if i.severity == "error"]
        note = (body.note if body else None) or None
        if errors and not note:
            raise HTTPException(409, f"Invoice still has {len(errors)} validation error(s); correct them or supply a note to override")
        inv.status, inv.needs_review, inv.review_note = "approved", False, note
        db.commit()
        return invoice_out(inv, inv.document.filename, detail=True)

    @api.get("/invoices")
    def list_invoices(
        db: DB,
        q: str | None = Query(None, max_length=200), supplier: str | None = None, customer: str | None = None,
        invoice_number: str | None = None, currency: str | None = Query(None, min_length=3, max_length=3),
        date_from: date | None = None, date_to: date | None = None,
        min_total: float | None = None, max_total: float | None = None,
        needs_review: bool | None = None, status: str | None = Query(None, pattern="^(extracted|corrected|approved)$"),
        document_id: str | None = None,
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
    ):
        stmt = search_mod.apply_filters(
            select(Invoice), q=q, supplier=supplier, customer=customer, invoice_number=invoice_number, currency=currency,
            date_from=date_from, date_to=date_to, min_total=min_total, max_total=max_total,
            needs_review=needs_review, status=status, document_id=document_id)
        total = db.scalar(select(func.count()).select_from(stmt.subquery()))
        rows = list(db.scalars(stmt.order_by(Invoice.created_at.desc()).limit(limit).offset(offset)))
        return {"total": total, "items": [invoice_out(i, i.document.filename, detail=True) for i in rows]}

    # ----------------------------------------------------------------- search
    @api.get("/search")
    def search(
        db: DB,
        q: str = Query(..., min_length=1, max_length=300),
        mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
        supplier: str | None = None, customer: str | None = None, currency: str | None = Query(None, min_length=3, max_length=3),
        date_from: date | None = None, date_to: date | None = None,
        min_total: float | None = None, max_total: float | None = None,
        needs_review: bool | None = None, limit: int = Query(20, ge=1, le=100),
    ):
        filt = dict(supplier=supplier, customer=customer, currency=currency, date_from=date_from, date_to=date_to,
                    min_total=min_total, max_total=max_total, needs_review=needs_review)
        scored: list[tuple[Invoice, float]] = []
        if mode == "keyword":
            stmt = search_mod.apply_filters(select(Invoice), q=q, **filt).order_by(Invoice.created_at.desc()).limit(500)
            rows = list(db.scalars(stmt))
            scored = sorted(((r, search_mod.keyword_score(r.search_text, q)) for r in rows), key=lambda t: -t[1])[:limit]
        else:
            base = search_mod.apply_filters(select(Invoice), **filt)
            hits = dict(search_mod.vector_search(db, svc.embedder, q, base, top_k=limit * 3 if mode == "hybrid" else limit))
            if mode == "hybrid":  # exact matches (an invoice number, say) may rank nowhere near the top by meaning
                for iid in db.scalars(search_mod.apply_filters(select(Invoice.id), q=q, **filt).limit(limit * 3)):
                    hits.setdefault(iid, 0.0)
            by_id = {i.id: i for i in db.scalars(select(Invoice).where(Invoice.id.in_(hits)))} if hits else {}
            for iid, sem in hits.items():
                inv = by_id.get(iid)
                if inv is None:
                    continue
                score = sem if mode == "semantic" else 0.6 * sem + 0.4 * search_mod.keyword_score(inv.search_text, q)
                scored.append((inv, score))
            scored = sorted(scored, key=lambda t: -t[1])[:limit]
        items = []
        for inv, score in scored:
            o = invoice_out(inv, inv.document.filename, detail=True)
            o["score"] = round(score, 4)
            o["snippet"] = _snippet(inv.search_text, q)
            items.append(o)
        return {"query": q, "mode": mode, "embedding_model": svc.embedder.name, "count": len(items), "items": items}

    # ----------------------------------------------------------------- cross-document reconciliation
    @api.get("/reconcile")
    def reconcile(db: DB, only_mismatches: bool = True, limit: int = Query(100, ge=1, le=500)):
        """Compare the same invoice (supplier + number) appearing in different documents: PDF vs PDF, PDF vs Excel..."""
        keys = select(Invoice.dedupe_key).where(Invoice.dedupe_key.is_not(None)).group_by(Invoice.dedupe_key) \
            .having(func.count(distinct(Invoice.document_id)) >= 2).limit(limit)
        rows = list(db.scalars(select(Invoice).where(Invoice.dedupe_key.in_(keys)).order_by(Invoice.dedupe_key)))
        groups: dict[str, list[Invoice]] = {}
        for r in rows:
            groups.setdefault(r.dedupe_key, []).append(r)
        out = []
        for key, recs in groups.items():
            diffs = []
            for f in ("total", "subtotal", "tax", "invoice_date", "currency", "customer_name"):
                vals = [(r, r.data.get(f)) for r in recs]
                norm = {(_cmp(f, v)) for _r, v in vals if v is not None}
                if len(norm) > 1:
                    diffs.append({"field": f, "values": [{"document_id": r.document_id, "filename": r.document.filename,
                                                          "invoice_id": r.id, "value": v} for r, v in vals]})
            n_items = {len(r.data.get("line_items") or []) for r in recs}
            if len(n_items) > 1:
                diffs.append({"field": "line_item_count", "values": [
                    {"document_id": r.document_id, "filename": r.document.filename, "invoice_id": r.id,
                     "value": len(r.data.get("line_items") or [])} for r in recs]})
            if only_mismatches and not diffs:
                continue
            out.append({"invoice_number": recs[0].invoice_number, "supplier_name": recs[0].supplier_name,
                        "consistent": not diffs, "differences": diffs,
                        "records": [{"invoice_id": r.id, "document_id": r.document_id, "filename": r.document.filename,
                                     "kind": r.document.kind} for r in recs]})
        return {"groups": out, "count": len(out)}

    app.include_router(api)
    return app


def _cmp(field: str, v):
    if field in ("total", "subtotal", "tax"):
        try:
            return f"{float(v):.2f}"
        except (TypeError, ValueError):
            return str(v)
    return re.sub(r"\s+", " ", str(v)).casefold()


def _snippet(text: str, query: str, width: int = 90) -> str:
    body = text.split("\n", 2)[-1] if "\n" in text else text
    low = body.casefold()
    for term in re.findall(r"\S+", query.casefold()):
        i = low.find(term)
        if i >= 0:
            s = max(0, i - width // 2)
            return re.sub(r"\s+", " ", body[s:s + width * 2]).strip()
    return re.sub(r"\s+", " ", body[:width * 2]).strip()
