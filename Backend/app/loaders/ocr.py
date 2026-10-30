"""OCR with Tesseract: preprocessing, rotation recovery, layout-preserving text."""
from __future__ import annotations

import shutil
from dataclasses import dataclass

import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

from .base import OcrUnavailableError
from .layout import Word, words_to_layout


@dataclass
class OcrResult:
    text: str
    confidence: float | None  # mean word confidence 0..100
    timed_out: bool = False


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


def to_gray(img: Image.Image) -> Image.Image:
    """Normalise any PIL mode to 8-bit grayscale (transparency flattened onto white)."""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba).convert("L")
    elif img.mode in ("I;16", "I;16L", "I;16B", "I"):
        img = img.point(lambda p: p * (1 / 256)).convert("L")  # type: ignore[arg-type]
    elif img.mode != "L":
        img = img.convert("L")
    return img


def estimate_skew(img: Image.Image, max_angle: float = 4.0) -> float:
    """Skew in degrees (projection-profile search on a small binarised copy). 0.0 when unsure.

    Text rows make the horizontal ink histogram spiky when the page is level, so the rotation that
    maximises its variance is the correction. Cheap: runs on a ~500px-wide thumbnail.
    """
    w, h = img.size
    scale = 500 / max(w, h)
    small = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)
    arr = np.asarray(small, dtype=np.uint8)
    thr = min(200, float(arr.mean()) - 25)  # ink is clearly darker than the page
    ink = Image.fromarray(((arr < thr) * 255).astype(np.uint8))
    if np.asarray(ink).mean() < 1.0:  # (almost) blank: nothing to align on
        return 0.0

    def score(angle: float) -> float:
        rot = ink.rotate(angle, resample=Image.BILINEAR, expand=False, fillcolor=0)
        prof = np.asarray(rot, dtype=np.float32).sum(axis=1)
        return float(np.var(prof))

    coarse = max(range(-8, 9), key=lambda i: score(i * max_angle / 8)) * max_angle / 8
    fine = [coarse + d for d in np.arange(-0.5, 0.51, 0.1)]
    best = max(fine, key=score)
    if score(best) <= score(0.0) * 1.03:  # no convincing improvement over "level"
        return 0.0
    return float(best)


def preprocess(img: Image.Image) -> Image.Image:
    img = to_gray(ImageOps.exif_transpose(img))
    w, h = img.size
    if max(w, h) < 1600:  # small / low-res scans OCR much better when upscaled
        scale = 1600 / max(w, h)
        img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
    img = ImageOps.autocontrast(img, cutoff=1)
    try:
        angle = estimate_skew(img)
        if abs(angle) >= 0.3:
            img = img.rotate(angle, resample=Image.BICUBIC, expand=False, fillcolor=255)
    except Exception:  # deskew is an optimisation; never let it fail a document
        pass
    return img.filter(ImageFilter.MedianFilter(3))


def _run(img: Image.Image, lang: str, timeout: int, psm: int) -> OcrResult:
    data = pytesseract.image_to_data(
        img, lang=lang, config=f"--psm {psm}", output_type=pytesseract.Output.DICT, timeout=timeout
    )
    words: list[Word] = []
    confs: list[float] = []
    for i, txt in enumerate(data["text"]):
        if not txt or not txt.strip():
            continue
        conf = float(data["conf"][i])
        if conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(x, y, x + w, y + h, txt.strip(), conf))
        confs.append(conf)
    mean = round(sum(confs) / len(confs), 1) if confs else None
    return OcrResult(words_to_layout(words), mean)


def ocr_image(img: Image.Image, *, lang: str = "eng", timeout: int = 90, psm: int = 6) -> OcrResult:
    """OCR one image. Retries with the orientation Tesseract detects if confidence is poor."""
    if not tesseract_available():
        raise OcrUnavailableError("Tesseract binary not found on PATH; install tesseract-ocr or set OCR_ENABLED=false")
    prepared = preprocess(img)
    try:
        best = _run(prepared, lang, timeout, psm)
    except RuntimeError as exc:  # pytesseract raises RuntimeError on timeout
        if "timeout" in str(exc).lower():
            return OcrResult("", None, timed_out=True)
        raise
    if best.confidence is None or best.confidence < 60:
        try:
            osd = pytesseract.image_to_osd(prepared, config="--psm 0", output_type=pytesseract.Output.DICT, timeout=30)
            angle = int(osd.get("rotate", 0))
            if angle:
                alt = _run(prepared.rotate(-angle, expand=True, fillcolor=255), lang, timeout, psm)
                if (alt.confidence or 0) > (best.confidence or 0):
                    best = alt
        except Exception:  # OSD fails on sparse pages; keep first pass
            pass
    return best
