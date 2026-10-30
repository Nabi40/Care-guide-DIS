"""Decide what a file really is (extension + magic bytes) before touching a parser."""
from __future__ import annotations

from pathlib import Path

from .base import UnsupportedFileError

EXT_KIND = {
    ".pdf": "pdf",
    ".jpg": "image", ".jpeg": "image", ".png": "image", ".tif": "image", ".tiff": "image",
    ".xlsx": "xlsx", ".xls": "xls", ".csv": "csv",
}
ALLOWED_EXTENSIONS = tuple(sorted(EXT_KIND))

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"
_TIFF = (b"II*\x00", b"MM\x00*")
_ZIP = b"PK\x03\x04"
_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def sniff_head(path: Path, n: int = 8192) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def detect_kind(head: bytes, filename: str) -> tuple[str, list[str]]:
    """Return (kind, warnings). Raises UnsupportedFileError for unknown or mismatching content."""
    ext = Path(filename).suffix.lower()
    if ext not in EXT_KIND:
        raise UnsupportedFileError(f"Unsupported file extension '{ext}'. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")
    if not head:
        raise UnsupportedFileError("File is empty")
    warnings: list[str] = []
    kind = EXT_KIND[ext]

    is_pdf = b"%PDF-" in head[:1024]
    is_img = head.startswith(_PNG) or head.startswith(_JPEG) or head.startswith(_TIFF)
    is_zip = head.startswith(_ZIP)
    is_ole = head.startswith(_OLE)

    if kind == "pdf":
        if not is_pdf:
            raise UnsupportedFileError("Content is not a PDF (missing %PDF header)")
    elif kind == "image":
        if not is_img:
            raise UnsupportedFileError("Content is not a PNG, JPEG or TIFF image")
    elif kind == "xlsx":
        if not is_zip:
            raise UnsupportedFileError("Content is not an .xlsx workbook")
    elif kind == "xls":
        if is_zip:  # a modern workbook saved with the old extension
            kind = "xlsx"
            warnings.append("File has .xls extension but is an .xlsx workbook")
        elif not is_ole:
            raise UnsupportedFileError("Content is not an .xls workbook")
    elif kind == "csv":
        has_bom16 = head.startswith((b"\xff\xfe", b"\xfe\xff"))
        if (b"\x00" in head and not has_bom16) or is_pdf or is_img or is_zip or is_ole:
            raise UnsupportedFileError("Content is not a text CSV file")
    return kind, warnings
