"""Keyword filtering + semantic (vector) search.

Real semantic vectors come from sentence-transformers. If that package or its model is unavailable,
EMBEDDING_BACKEND=auto falls back to a dependency-free hashing embedder, which only gives typo/partial-word
tolerant similarity, NOT meaning. For millions of rows swap `vector_search` for pgvector / Qdrant; the
interface is tiny.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import zlib
from datetime import date
from typing import Protocol

import numpy as np
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from .db import Invoice

log = logging.getLogger(__name__)


class Embedder(Protocol):
    name: str
    dim: int
    min_score: float  # vector hits below this are noise, not results

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    """Signed feature hashing over word unigrams, bigrams and character trigrams."""
    name = "hashing-512-v1"
    min_score = -1.0

    def __init__(self, dim: int = 512):
        self.dim = dim

    def _features(self, text: str):
        toks = re.findall(r"[a-z0-9]+", text.casefold())
        for t in toks:
            yield t, 1.0
            if len(t) >= 4:
                p = f"#{t}#"
                for i in range(len(p) - 2):
                    yield "c:" + p[i:i + 3], 0.35
        for a, b in zip(toks, toks[1:]):
            yield f"b:{a}_{b}", 0.5

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, text in enumerate(texts):
            counts: dict[str, float] = {}
            for f, w in self._features(text):
                counts[f] = counts.get(f, 0.0) + w
            for f, w in counts.items():
                h = zlib.crc32(f.encode())
                out[r, h % self.dim] += (1.0 if (h >> 16) & 1 else -1.0) * math.log1p(w)
            n = np.linalg.norm(out[r])
            if n > 0:
                out[r] /= n
        return out


class SentenceTransformerEmbedder:
    min_score = 0.2

    def __init__(self, model: str):
        from sentence_transformers import SentenceTransformer  # heavy import: only when this backend is used

        self._m = SentenceTransformer(model)
        self._lock = threading.Lock()  # the fast tokenizer is not safe across worker + API threads
        self.name = model
        self.dim = int(self.embed(["dimension probe"]).shape[1])

    def embed(self, texts: list[str]) -> np.ndarray:
        with self._lock:
            return np.asarray(self._m.encode(texts, normalize_embeddings=True, show_progress_bar=False), dtype=np.float32)


def make_embedder(backend: str, model: str) -> Embedder:
    if backend == "sentence-transformers":
        return SentenceTransformerEmbedder(model)
    if backend == "auto":
        try:
            return SentenceTransformerEmbedder(model)
        except Exception as exc:  # package missing, or model not downloadable: degrade, but say so
            log.warning("Semantic embeddings unavailable (%s); falling back to hashing (typo tolerance only)", exc)
    return HashingEmbedder()


def embedding_text(data: dict, body: str) -> str:
    """What the vector represents. Models read only the first ~256 tokens, so meaning goes first:
    who and what was bought. Numbers, dates and file names carry no semantic signal (keyword search has them)."""
    items = "; ".join(li["description"] for li in (data.get("line_items") or [])[:50] if li.get("description"))
    head = f"Invoice from {data.get('supplier_name') or 'unknown supplier'} to {data.get('customer_name') or 'unknown customer'}."
    return f"{head} Items: {items}.\n{body[:2000]}" if items else f"{head}\n{body[:2000]}"


def search_text_body(search_text: str) -> str:
    """The raw document text stored after the two header lines of `invoice_search_text`."""
    parts = (search_text or "").split("\n", 2)
    return parts[2] if len(parts) > 2 else ""


def invoice_search_text(data: dict, body: str, filename: str = "") -> str:
    items = " ; ".join(
        f"{li.get('description') or ''} {li.get('amount') or ''}" for li in (data.get("line_items") or [])[:50]
    )
    head = (
        f"invoice {data.get('invoice_number') or ''} supplier {data.get('supplier_name') or ''} "
        f"customer {data.get('customer_name') or ''} date {data.get('invoice_date') or ''} "
        f"total {data.get('total') or ''} {data.get('currency') or ''} file {filename}"
    )
    return f"{head}\nitems: {items}\n{body[:20000]}"


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# --------------------------------------------------------------------------- filters
def _like_escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_filters(
    stmt: Select,
    *,
    q: str | None = None,
    supplier: str | None = None,
    customer: str | None = None,
    invoice_number: str | None = None,
    currency: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    min_total: float | None = None,
    max_total: float | None = None,
    needs_review: bool | None = None,
    status: str | None = None,
    document_id: str | None = None,
) -> Select:
    if q:
        for term in re.findall(r"\S+", q.casefold())[:10]:
            stmt = stmt.where(func.lower(Invoice.search_text).like(f"%{_like_escape(term)}%", escape="\\"))
    if supplier:
        stmt = stmt.where(func.lower(Invoice.supplier_name).like(f"%{_like_escape(supplier.casefold())}%", escape="\\"))
    if customer:
        stmt = stmt.where(func.lower(Invoice.customer_name).like(f"%{_like_escape(customer.casefold())}%", escape="\\"))
    if invoice_number:
        stmt = stmt.where(func.lower(Invoice.invoice_number).like(f"%{_like_escape(invoice_number.casefold())}%", escape="\\"))
    if currency:
        stmt = stmt.where(Invoice.currency == currency.upper())
    if date_from:
        stmt = stmt.where(Invoice.invoice_date >= date_from)
    if date_to:
        stmt = stmt.where(Invoice.invoice_date <= date_to)
    if min_total is not None:
        stmt = stmt.where(Invoice.total >= min_total)
    if max_total is not None:
        stmt = stmt.where(Invoice.total <= max_total)
    if needs_review is not None:
        stmt = stmt.where(Invoice.needs_review == needs_review)
    if status:
        stmt = stmt.where(Invoice.status == status)
    if document_id:
        stmt = stmt.where(Invoice.document_id == document_id)
    return stmt


def vector_search(
    session: Session, embedder: Embedder, query: str, filters_stmt: Select, top_k: int, max_candidates: int = 50_000
) -> list[tuple[str, float]]:
    """Cosine similarity over (filtered) invoices. Returns [(invoice_id, score)]."""
    rows = session.execute(
        filters_stmt.with_only_columns(Invoice.id, Invoice.embedding, Invoice.embedding_model)
        .where(Invoice.embedding.is_not(None), Invoice.embedding_model == embedder.name)
        .limit(max_candidates)
    ).all()
    if not rows:
        return []
    mat = np.vstack([from_blob(r.embedding) for r in rows])
    qv = embedder.embed([query])[0]
    scores = mat @ qv
    k = min(top_k, len(rows))
    top = np.argpartition(-scores, k - 1)[:k] if k < len(rows) else np.arange(len(rows))
    top = top[np.argsort(-scores[top])]
    return [(rows[i].id, float(scores[i])) for i in top if scores[i] >= embedder.min_score]


STOPWORDS = frozenset("a an and are at by for from in is it of on or the to with".split())


def keyword_score(text: str, query: str) -> float:
    words = re.findall(r"\S+", query.casefold())
    terms = [w for w in words if w not in STOPWORDS] or words  # "for" is in every document: it says nothing
    if not terms:
        return 0.0
    low = text.casefold()
    return sum(1 for t in terms if t in low) / len(terms)
