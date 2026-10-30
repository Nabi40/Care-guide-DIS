from __future__ import annotations

from pathlib import Path

from ..config import Settings
from .base import LoadedDocument, ProcessingError, ProgressFn
from .detect import ALLOWED_EXTENSIONS, detect_kind, sniff_head
from .image import load_image
from .pdf import load_pdf
from .spreadsheet import load_spreadsheet

__all__ = ["load_document", "detect_kind", "sniff_head", "ALLOWED_EXTENSIONS", "LoadedDocument", "ProcessingError"]


def load_document(path: Path, filename: str, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    kind, warnings = detect_kind(sniff_head(path), filename)
    if kind == "pdf":
        doc = load_pdf(path, settings, progress)
    elif kind == "image":
        doc = load_image(path, settings, progress)
    else:
        doc = load_spreadsheet(kind, path, settings, progress)
    doc.warnings = warnings + doc.warnings
    return doc
