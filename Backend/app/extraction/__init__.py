"""Extraction orchestration: rules first, LLM only to fill gaps, grounded and merged."""
from __future__ import annotations

from ..config import Settings
from ..schemas import REQUIRED_FIELDS, SCALAR_FIELDS, ExtractionResult, FieldMeta
from . import rules
from .llm import LLMClient, llm_extract


def _same(field: str, a, b) -> bool:
    if field in ("subtotal", "tax", "total"):
        return abs(a - b) <= 0.005
    if isinstance(a, str) and isinstance(b, str):
        from ..utils import norm_text
        return norm_text(a) == norm_text(b)
    return a == b


def extract_text(text: str, settings: Settings, llm: LLMClient | None = None) -> ExtractionResult:
    """Extract from page text (digital PDF or OCR)."""
    res = rules.extract(text, dayfirst=settings.date_dayfirst)
    return enrich_with_llm(res, text, settings, llm)


def enrich_with_llm(res: ExtractionResult, text: str, settings: Settings, llm: LLMClient | None) -> ExtractionResult:
    if llm is None or not text.strip():
        return res
    weak = [
        f for f in REQUIRED_FIELDS + ["currency", "customer_name"]
        if getattr(res.data, f) is None or res.meta.get(f, FieldMeta()).confidence < 0.8
    ]
    if not weak and res.data.line_items:
        return res
    if len(text) > settings.llm_max_chars:
        res.warnings.append(f"LLM saw only the first {settings.llm_max_chars} of {len(text)} characters")
    out, dropped = llm_extract(llm, text, max_chars=settings.llm_max_chars, dayfirst=settings.date_dayfirst)
    if out is None:
        res.warnings.append(f"LLM extraction skipped ({dropped[0]})")
        return res
    if dropped:
        res.warnings.append(f"LLM values not found in the source were discarded: {', '.join(sorted(set(dropped)))}")
    for f in SCALAR_FIELDS:
        lv = getattr(out.data, f)
        if lv is None:
            continue
        rv = getattr(res.data, f)
        rconf = res.meta.get(f, FieldMeta()).confidence
        if rv is None or rconf < 0.8:
            if rv is not None and not _same(f, rv, lv):
                res.flags.append("LLM_DISAGREEMENT")
                res.warnings.append(f"{f}: rules read '{rv}', LLM read '{lv}'")
                res.meta[f] = FieldMeta(confidence=0.4, source="rules", evidence=res.meta[f].evidence)
                continue
            setattr(res.data, f, lv)
            res.meta[f] = out.meta[f]
            if f == "invoice_date":
                res.ambiguous_date = False
        elif not _same(f, rv, lv):
            res.flags.append("LLM_DISAGREEMENT")
            res.warnings.append(f"{f}: rules read '{rv}', LLM read '{lv}'")
            res.meta[f] = FieldMeta(confidence=min(rconf, 0.5), source="rules", evidence=res.meta[f].evidence)
    if not res.data.line_items and out.data.line_items:
        res.data.line_items = out.data.line_items
        res.meta["line_items"] = out.meta["line_items"]
    return res
