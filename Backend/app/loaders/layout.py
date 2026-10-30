"""Turn positioned words (from a PDF text layer or from OCR) into layout-preserving text.

Invoices are tables and side-by-side blocks; plain reading order destroys the
label/value relationships. This renders words on a character grid (like
`pdftotext -layout`) so downstream rules can reason about columns.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    conf: float | None = None


def words_to_layout(words: list[Word], max_cols: int = 200) -> str:
    words = [w for w in words if w.text and w.text.strip()]
    if not words:
        return ""
    med_h = median(max(w.y1 - w.y0, 1e-6) for w in words)
    widths = [(w.x1 - w.x0) / len(w.text) for w in words if len(w.text) >= 2 and w.x1 > w.x0]
    char_w = median(widths) if widths else med_h * 0.5
    char_w = max(char_w, 1e-6)
    x_min = min(w.x0 for w in words)
    x_max = max(w.x1 for w in words)
    if (x_max - x_min) / char_w > max_cols:
        char_w = (x_max - x_min) / max_cols

    # cluster into visual lines by vertical centre
    ordered = sorted(words, key=lambda w: (w.y0 + w.y1) / 2)
    lines: list[list[Word]] = []
    centres: list[float] = []
    for w in ordered:
        cy = (w.y0 + w.y1) / 2
        if lines and abs(cy - centres[-1]) <= 0.55 * med_h:
            lines[-1].append(w)
            centres[-1] = sum((x.y0 + x.y1) / 2 for x in lines[-1]) / len(lines[-1])
        else:
            lines.append([w])
            centres.append(cy)

    out: list[str] = []
    prev_c: float | None = None
    for line, c in zip(lines, centres):
        if prev_c is not None and c - prev_c > 2.4 * med_h:
            out.append("")  # visual paragraph break
        prev_c = c
        buf = ""
        prev: Word | None = None
        line_h = median(max(w.y1 - w.y0, 1e-6) for w in line)
        for w in sorted(line, key=lambda x: x.x0):
            if prev is not None and (w.x0 - prev.x1) <= 0.7 * line_h:
                # words of one phrase (normal inter-word space, any font size): exactly one space
                buf += " " + w.text
                prev = w
                continue
            target = int(round((w.x0 - x_min) / char_w))
            if buf:
                target = max(target, len(buf) + 3)  # separate columns stay >= 3 spaces apart
            buf += " " * (target - len(buf)) + w.text
            prev = w
        out.append(buf.rstrip())
    return "\n".join(out)
