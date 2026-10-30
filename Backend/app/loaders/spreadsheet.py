"""XLSX / XLS / CSV loaders. Produce raw cell grids plus aligned text for search."""
from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import Settings
from .base import (
    CorruptedFileError, EncryptedFileError, LimitExceededError, LoadedDocument,
    PageContent, ProgressFn, Sheet, UnsupportedFileError,
)

MAX_UNCOMPRESSED = 600 * 1024 * 1024  # zip-bomb guard for xlsx


def fmt_cell(v: Any) -> str:
    """Stable text form of a cell value."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0, 0) else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return ""
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return format(Decimal(repr(round(v, 6))), "f")
    return re.sub(r"\s+", " ", str(v)).strip()


def _trim(rows: list[list[Any]]) -> list[list[Any]]:
    out = []
    for r in rows:
        r = list(r)
        while r and (r[-1] is None or (isinstance(r[-1], str) and not r[-1].strip())):
            r.pop()
        out.append(r)
    while out and not out[-1]:
        out.pop()
    return out


def sheet_to_text(rows: list[list[Any]], max_width: int = 40) -> str:
    """Column-aligned text so label/value pairs line up like a printed page."""
    cells = [[fmt_cell(c) for c in r] for r in rows]
    ncols = max((len(r) for r in cells), default=0)
    widths = [0] * ncols
    for r in cells:
        for j, c in enumerate(r):
            widths[j] = min(max(widths[j], len(c)), max_width)
    lines = []
    for r in cells:
        if not any(r):
            lines.append("")
            continue
        lines.append("   ".join(c.ljust(widths[j]) for j, c in enumerate(r)).rstrip())
    return "\n".join(lines)


def _finish(kind: str, sheets: list[Sheet], warnings: list[str]) -> LoadedDocument:
    doc = LoadedDocument(kind=kind, warnings=warnings)
    for i, sh in enumerate(sheets, start=1):
        sh.rows = _trim(sh.rows)
        doc.sheets.append(sh)
        doc.pages.append(PageContent(i, sheet_to_text(sh.rows), "sheet", None, label=sh.name))
    if not any(s.rows for s in doc.sheets):
        raise CorruptedFileError("Spreadsheet contains no data")
    return doc


def load_xlsx(path: Path, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    import openpyxl

    try:
        with zipfile.ZipFile(path) as zf:
            total = sum(i.file_size for i in zf.infolist())
            if total > MAX_UNCOMPRESSED:
                raise LimitExceededError("Workbook expands to an unreasonable size")
            names = zf.namelist()
            if "xl/workbook.xml" not in names:
                if "EncryptedPackage" in names:
                    raise EncryptedFileError("Workbook is password protected")
                raise CorruptedFileError("Not a valid .xlsx workbook (xl/workbook.xml missing)")
    except zipfile.BadZipFile as exc:
        raise CorruptedFileError(f"Workbook is corrupted: {exc}") from exc

    warnings: list[str] = []
    sheets: list[Sheet] = []
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise CorruptedFileError(f"Cannot open workbook: {exc}") from exc
    try:
        names = wb.sheetnames
        for n, name in enumerate(names, start=1):
            ws = wb[name]
            rows: list[list[Any]] = []
            truncated = False
            try:
                for r in ws.iter_rows(values_only=True):
                    if len(rows) >= settings.max_sheet_rows:
                        truncated = True
                        break
                    rows.append(list(r))
            except Exception as exc:
                warnings.append(f"Sheet '{name}' could not be fully read: {exc}")
            if truncated:
                warnings.append(f"Sheet '{name}' truncated at {settings.max_sheet_rows} rows")
            sheets.append(Sheet(name, rows))
            if progress:
                progress(n, len(names))
    finally:
        wb.close()
    doc = _finish("xlsx", sheets, warnings)
    empty_formula = sum(
        1 for s in doc.sheets for r in s.rows for c in r if isinstance(c, str) and c.startswith("=")
    )
    if empty_formula:
        doc.warnings.append(f"{empty_formula} formula cell(s) have no cached value; open and re-save in Excel")
    return doc


def load_xls(path: Path, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    import xlrd

    try:
        book = xlrd.open_workbook(path, on_demand=True)
    except xlrd.biffh.XLRDError as exc:
        if "password" in str(exc).lower() or "encrypt" in str(exc).lower():
            raise EncryptedFileError("Workbook is password protected") from exc
        raise CorruptedFileError(f"Cannot open .xls workbook: {exc}") from exc
    except Exception as exc:
        raise CorruptedFileError(f"Cannot open .xls workbook: {exc}") from exc
    sheets: list[Sheet] = []
    warnings: list[str] = []
    try:
        for n in range(book.nsheets):
            sh = book.sheet_by_index(n)
            rows: list[list[Any]] = []
            for r in range(min(sh.nrows, settings.max_sheet_rows)):
                row: list[Any] = []
                for c in range(sh.ncols):
                    cell = sh.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            row.append(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode))
                        except Exception:
                            row.append(None)
                    elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
                        row.append(None)
                    else:
                        row.append(cell.value)
                rows.append(row)
            if sh.nrows > settings.max_sheet_rows:
                warnings.append(f"Sheet '{sh.name}' truncated at {settings.max_sheet_rows} rows")
            sheets.append(Sheet(sh.name, rows))
            if progress:
                progress(n + 1, book.nsheets)
    finally:
        book.release_resources()
    return _finish("xls", sheets, warnings)


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1"), "latin-1"


def load_csv(path: Path, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    raw = path.read_bytes()
    if not raw.strip():
        raise CorruptedFileError("CSV file is empty")
    try:
        text, enc = _decode(raw)
    except UnicodeError as exc:
        raise CorruptedFileError(f"Cannot decode CSV: {exc}") from exc
    sample = text[:8192]
    delim = ","
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in (",", ";", "\t", "|")}
        delim = max(counts, key=counts.get) if any(counts.values()) else ","
    csv.field_size_limit(10_000_000)
    rows: list[list[Any]] = []
    warnings: list[str] = []
    try:
        for r in csv.reader(io.StringIO(text, newline=""), delimiter=delim):
            if len(rows) >= settings.max_sheet_rows:
                warnings.append(f"CSV truncated at {settings.max_sheet_rows} rows")
                break
            rows.append([c.strip() if c.strip() else None for c in r])
    except csv.Error as exc:
        raise CorruptedFileError(f"Malformed CSV: {exc}") from exc
    if enc not in ("utf-8-sig",):
        warnings.append(f"CSV decoded as {enc}")
    return _finish("csv", [Sheet("csv", rows)], warnings)


def load_spreadsheet(kind: str, path: Path, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    if kind == "xlsx":
        return load_xlsx(path, settings, progress)
    if kind == "xls":
        return load_xls(path, settings, progress)
    if kind == "csv":
        return load_csv(path, settings, progress)
    raise UnsupportedFileError(kind)
