"""Shared loader types and errors."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


class ProcessingError(Exception):
    code = "processing_error"
    retryable = False


class CorruptedFileError(ProcessingError):
    code = "corrupted_file"


class EncryptedFileError(ProcessingError):
    code = "encrypted_file"


class UnsupportedFileError(ProcessingError):
    code = "unsupported_type"


class LimitExceededError(ProcessingError):
    code = "limit_exceeded"


class OcrUnavailableError(ProcessingError):
    code = "ocr_unavailable"


class TransientError(ProcessingError):
    code = "transient_error"
    retryable = True


@dataclass
class PageContent:
    page_no: int  # 1-based page / frame / sheet index
    text: str
    method: str  # digital | ocr | sheet
    confidence: float | None = None  # mean OCR word confidence 0..100
    label: str | None = None  # sheet name


@dataclass
class Sheet:
    name: str
    rows: list[list[Any]]


@dataclass
class LoadedDocument:
    kind: str
    pages: list[PageContent] = field(default_factory=list)
    sheets: list[Sheet] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ocr_pages(self) -> int:
        return sum(1 for p in self.pages if p.method == "ocr")

    @property
    def text_method(self) -> str:
        methods = {p.method for p in self.pages}
        if methods == {"sheet"}:
            return "sheet"
        if methods == {"ocr"}:
            return "ocr"
        if methods == {"digital"}:
            return "digital"
        return "mixed"

    @property
    def avg_ocr_confidence(self) -> float | None:
        vals = [p.confidence for p in self.pages if p.method == "ocr" and p.confidence is not None]
        return round(sum(vals) / len(vals), 1) if vals else None


ProgressFn = Callable[[int, int], None]  # (done, total)
