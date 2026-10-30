"""Database schema (SQLAlchemy 2.0). SQLite by default, PostgreSQL via DATABASE_URL."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, LargeBinary,
    String, Text, create_engine, event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Document(Base):
    """One uploaded file and its processing state."""
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(String(255))  # original name, display only
    stored_name: Mapped[str] = mapped_column(String(80))  # uuid-based name on disk
    kind: Mapped[str] = mapped_column(String(10))  # pdf | image | xlsx | xls | csv
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(12), index=True, default="queued")  # queued|processing|completed|failed
    error_code: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    page_count: Mapped[int] = mapped_column(Integer, default=0)
    pages_done: Mapped[int] = mapped_column(Integer, default=0)
    ocr_pages: Mapped[int] = mapped_column(Integer, default=0)
    text_method: Mapped[str | None] = mapped_column(String(10))  # digital | ocr | mixed | sheet
    avg_ocr_confidence: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    processing_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    pages: Mapped[list["PageText"]] = relationship(cascade="all, delete-orphan", back_populates="document")
    invoices: Mapped[list["Invoice"]] = relationship(cascade="all, delete-orphan", back_populates="document")
    events: Mapped[list["ProcessingEvent"]] = relationship(
        cascade="all, delete-orphan", back_populates="document", order_by="ProcessingEvent.id"
    )


class PageText(Base):
    """Extracted text per PDF page / image frame / sheet."""
    __tablename__ = "page_texts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    page_no: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(255))  # sheet name
    method: Mapped[str] = mapped_column(String(10))  # digital | ocr | sheet
    confidence: Mapped[float | None] = mapped_column(Float)
    text: Mapped[str] = mapped_column(Text)
    document: Mapped[Document] = relationship(back_populates="pages")


class Invoice(Base):
    """One extracted invoice (a file may contain many)."""
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    segment_index: Mapped[int] = mapped_column(Integer, default=0)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)

    # canonical structured result (decimals as strings) + per-field confidence / evidence
    data: Mapped[dict] = mapped_column(JSON)
    field_meta: Mapped[dict] = mapped_column(JSON, default=dict)

    # denormalised, indexed columns for filtering (kept in sync with `data`)
    invoice_number: Mapped[str | None] = mapped_column(String(300), index=True)
    number_key: Mapped[str | None] = mapped_column(String(300), index=True)
    supplier_name: Mapped[str | None] = mapped_column(String(300))
    supplier_key: Mapped[str | None] = mapped_column(String(300), index=True)
    customer_name: Mapped[str | None] = mapped_column(String(300))
    invoice_date: Mapped[date | None] = mapped_column(Date, index=True)
    currency: Mapped[str | None] = mapped_column(String(3), index=True)
    total: Mapped[float | None] = mapped_column(Float, index=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(700), index=True)

    status: Mapped[str] = mapped_column(String(12), default="extracted")  # extracted|corrected|approved
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    review_note: Mapped[str | None] = mapped_column(Text)

    search_text: Mapped[str] = mapped_column(Text, default="")
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding_model: Mapped[str | None] = mapped_column(String(80))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    document: Mapped[Document] = relationship(back_populates="invoices")
    issues: Mapped[list["ValidationIssue"]] = relationship(
        cascade="all, delete-orphan", back_populates="invoice", order_by="ValidationIssue.id"
    )
    corrections: Mapped[list["Correction"]] = relationship(
        cascade="all, delete-orphan", back_populates="invoice", order_by="Correction.id"
    )

    __table_args__ = (Index("ix_invoice_supplier_date", "supplier_key", "invoice_date"),)


class ValidationIssue(Base):
    __tablename__ = "validation_issues"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(40), index=True)
    severity: Mapped[str] = mapped_column(String(8))  # error | warning
    field: Mapped[str | None] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    invoice: Mapped[Invoice] = relationship(back_populates="issues")


class Correction(Base):
    """Audit trail of manual edits."""
    __tablename__ = "corrections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id", ondelete="CASCADE"), index=True)
    field: Mapped[str] = mapped_column(String(40))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    invoice: Mapped[Invoice] = relationship(back_populates="corrections")


class ProcessingEvent(Base):
    """Processing history: one row per pipeline stage per attempt."""
    __tablename__ = "processing_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(10))  # ok | error | warning
    message: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    document: Mapped[Document] = relationship(back_populates="events")


def make_engine(url: str) -> Engine:
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
