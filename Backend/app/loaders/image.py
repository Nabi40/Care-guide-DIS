"""JPG / PNG / TIFF loader (multi-frame TIFF supported) via OCR."""
from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageSequence, UnidentifiedImageError

from ..config import Settings
from .base import (
    CorruptedFileError, LimitExceededError, LoadedDocument, OcrUnavailableError,
    PageContent, ProgressFn,
)
from .ocr import ocr_image, tesseract_available

log = logging.getLogger(__name__)


def load_image(path: Path, settings: Settings, progress: ProgressFn | None = None) -> LoadedDocument:
    if not settings.ocr_enabled:
        raise OcrUnavailableError("OCR is disabled (OCR_ENABLED=false) so images cannot be read")
    if not tesseract_available():
        raise OcrUnavailableError("Tesseract binary not found on PATH")
    Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
    try:
        with Image.open(path) as probe:
            probe.verify()  # catches truncated / garbage files early
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise CorruptedFileError(f"Cannot decode image: {exc}") from exc
    except Image.DecompressionBombError as exc:
        raise LimitExceededError(f"Image is too large: {exc}") from exc

    result = LoadedDocument(kind="image")
    try:
        with Image.open(path) as img:
            frames = getattr(img, "n_frames", 1)
            if frames > settings.max_pdf_pages:
                raise LimitExceededError(f"Image has {frames} frames; limit is {settings.max_pdf_pages}")
            for n, frame in enumerate(ImageSequence.Iterator(img), start=1):
                try:
                    res = ocr_image(frame.copy(), lang=settings.ocr_lang, timeout=settings.ocr_timeout_s)
                except OSError as exc:  # truncated pixel data surfaces here
                    raise CorruptedFileError(f"Image data is damaged: {exc}") from exc
                if res.timed_out:
                    result.warnings.append(f"OCR timed out on frame {n}")
                result.pages.append(PageContent(n, res.text, "ocr", res.confidence))
                if progress:
                    progress(n, frames)
    except Image.DecompressionBombError as exc:
        raise LimitExceededError(f"Image is too large: {exc}") from exc
    except (UnidentifiedImageError, SyntaxError, ValueError) as exc:
        raise CorruptedFileError(f"Cannot decode image: {exc}") from exc

    low = [p.page_no for p in result.pages if (p.confidence or 0) < 50]
    if low:
        result.warnings.append(f"Low OCR confidence on frame(s) {low[:10]}; poor image quality")
    return result
