"""PDF loader: text layer first, OCR only for pages that need it."""
from __future__ import annotations

import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from PIL import Image

from ..config import Settings
from .base import (
    CorruptedFileError, EncryptedFileError, LimitExceededError, LoadedDocument,
    PageContent, ProgressFn,
)
from .layout import Word, words_to_layout
from .ocr import ocr_image

log = logging.getLogger(__name__)
MAX_RENDER_PIXELS = 25_000_000
_PDFIUM_LOCK = threading.Lock()  # PDFium is not thread-safe, even across separate documents, and job workers run in parallel


def _alnum_count(text: str) -> int:
    return sum(ch.isalnum() for ch in text)


def _page_words(page: pdfium.PdfPage) -> list[Word]:
    """Group PDFium's characters into positioned words (top-left origin, like the layout grid expects)."""
    height = page.get_height()
    textpage = page.get_textpage()
    words: list[Word] = []
    chars: list[str] = []
    x0 = top = x1 = bottom = 0.0

    def flush() -> None:
        if chars:
            words.append(Word(x0, top, x1, bottom, "".join(chars)))
            chars.clear()

    try:
        for i in range(textpage.count_chars()):
            code = pdfium_c.FPDFText_GetUnicode(textpage, i)
            if code in (0x02, 0xFFFE):  # PDFium's markers for a hyphen
                code = ord("-")
            ch = chr(code)
            if code == 0 or 0xD800 <= code <= 0xDFFF or ch.isspace():
                flush()
                continue
            left, low, right, high = textpage.get_charbox(i, loose=True)  # tight boxes make "-" and "," look tiny
            char_top, char_bottom = height - high, height - low
            size = char_bottom - char_top
            if chars and (left - x1 > 0.5 * size or left < x1 - size or char_top > bottom - 0.3 * size):
                flush()  # a jump that PDFium did not mark with a space: next column or next line
            if chars:
                x1, top, bottom = max(x1, right), min(top, char_top), max(bottom, char_bottom)
            else:
                x0, top, x1, bottom = left, char_top, right, char_bottom
            chars.append(ch)
        flush()
    finally:
        textpage.close()
    return words


def _digital_text(page: pdfium.PdfPage) -> str:
    return words_to_layout(_page_words(page))


def _render(page: pdfium.PdfPage, dpi: int) -> Image.Image:
    width, height = page.get_size()
    px = (width / 72 * dpi) * (height / 72 * dpi)
    if px > MAX_RENDER_PIXELS:  # absurdly large page: scale the DPI down
        dpi = max(72, int(dpi * (MAX_RENDER_PIXELS / px) ** 0.5))
    return page.render(scale=dpi / 72, grayscale=True).to_pil().convert("L")


def _page_needs_ocr(page: pdfium.PdfPage, text: str, min_chars: int) -> bool:
    chars = _alnum_count(text)
    if chars < min_chars:
        return True
    if chars < 150:  # little text but big embedded picture => probably a scan with a stamp/header
        try:
            width, height = page.get_size()
            covered = 0.0
            for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE]):
                x0, y0, x1, y1 = obj.get_bounds()
                covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
            return covered > 0.5 * width * height
        except Exception:
            return False
    return False


def _read_page(doc: pdfium.PdfDocument, i: int, min_chars: int) -> tuple[str, bool]:
    with _PDFIUM_LOCK:
        page = doc[i]
        try:
            text = _digital_text(page)
            return text, _page_needs_ocr(page, text, min_chars)
        finally:
            page.close()


def _render_page(doc: pdfium.PdfDocument, i: int, dpi: int) -> Image.Image:
    with _PDFIUM_LOCK:
        page = doc[i]
        try:
            return _render(page, dpi)
        finally:
            page.close()


def load_pdf(path: Path, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    try:
        with _PDFIUM_LOCK:
            doc = pdfium.PdfDocument(path)
    except pdfium.PdfiumError as exc:
        if exc.err_code == pdfium_c.FPDF_ERR_PASSWORD:
            raise EncryptedFileError("PDF is password protected") from exc
        raise CorruptedFileError(f"Cannot open PDF: {exc}") from exc
    except Exception as exc:
        raise CorruptedFileError(f"Cannot open PDF: {exc}") from exc
    try:
        total = len(doc)
        if total == 0:
            raise CorruptedFileError("PDF has no pages")
        if total > settings.max_pdf_pages:
            raise LimitExceededError(f"PDF has {total} pages; limit is {settings.max_pdf_pages}")

        result = LoadedDocument(kind="pdf")
        texts: dict[int, str] = {}
        failed: list[int] = []
        need_ocr: list[int] = []

        for i in range(total):
            try:
                text, ocr_needed = _read_page(doc, i, settings.min_text_chars_per_page)
                texts[i] = text
                if ocr_needed:
                    need_ocr.append(i)
            except Exception as exc:  # one broken page must not kill a 1000-page file
                log.warning("page %s unreadable: %s", i + 1, exc)
                failed.append(i)
                texts[i] = ""
            if progress and (i % 25 == 0):
                progress(i + 1, total)

        ocr_out: dict[int, tuple[str, float | None]] = {}
        if need_ocr and settings.ocr_enabled:
            done = 0
            batch = max(2, settings.ocr_workers * 2)
            with ThreadPoolExecutor(max_workers=settings.ocr_workers) as pool:
                for start in range(0, len(need_ocr), batch):
                    idx = need_ocr[start:start + batch]
                    images = []
                    for i in idx:  # rendering stays on this thread; only OCR of the finished images is parallel
                        try:
                            images.append(_render_page(doc, i, settings.ocr_dpi))
                        except Exception as exc:
                            log.warning("render page %s failed: %s", i + 1, exc)
                            images.append(None)
                            failed.append(i)

                    def work(img: Image.Image | None):
                        if img is None:
                            return None
                        return ocr_image(img, lang=settings.ocr_lang, timeout=settings.ocr_timeout_s)

                    for i, res in zip(idx, pool.map(work, images)):
                        if res is None:
                            continue
                        if res.timed_out:
                            result.warnings.append(f"OCR timed out on page {i + 1}")
                        ocr_out[i] = (res.text, res.confidence)
                    done += len(idx)
                    if progress:
                        progress(min(total, total - len(need_ocr) + done), total)
        elif need_ocr:
            result.warnings.append(f"{len(need_ocr)} page(s) have no text layer and OCR is disabled")

        for i in range(total):
            if i in ocr_out:
                otext, conf = ocr_out[i]
                dtext = texts.get(i, "")
                use_ocr = _alnum_count(otext) >= _alnum_count(dtext)
                result.pages.append(
                    PageContent(i + 1, otext if use_ocr else dtext, "ocr" if use_ocr else "digital", conf if use_ocr else None)
                )
            else:
                result.pages.append(PageContent(i + 1, texts.get(i, ""), "digital"))

        if failed:
            result.warnings.append(f"Could not read page(s): {', '.join(str(i + 1) for i in sorted(set(failed))[:20])}")
        if len(set(failed)) == total:
            raise CorruptedFileError("None of the PDF pages could be read")
        if progress:
            progress(total, total)
        return result
    finally:
        with _PDFIUM_LOCK:
            doc.close()


def image_to_pdf_bytes(img: Image.Image) -> bytes:  # used by tests / eval to build scanned PDFs
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PDF", resolution=200)
    return buf.getvalue()
