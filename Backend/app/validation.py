"""Validation rules. Pure function: structured data in, list of issues out."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from .config import Settings
from .schemas import REQUIRED_FIELDS, ExtractionResult, FieldMeta, InvoiceData
from .utils import money

ERROR, WARNING = "error", "warning"
# warnings that still force a human look
REVIEW_WARNING_CODES = {"LOW_CONFIDENCE", "AMBIGUOUS_DATE", "LLM_DISAGREEMENT", "MISSING_FIELD", "TAX_RATE_UNUSUAL", "SOURCE_INCONSISTENT"}

_MISSING_SEVERITY = {
    "invoice_number": ERROR, "invoice_date": ERROR, "supplier_name": ERROR, "total": ERROR,
    "customer_name": WARNING, "currency": WARNING, "subtotal": WARNING, "tax": WARNING,
}


@dataclass
class Issue:
    code: str
    severity: str
    field: str | None
    message: str


def _fmt(d: Decimal) -> str:
    return f"{money(d):,.2f}"


def validate(
    data: InvoiceData,
    meta: dict[str, FieldMeta],
    settings: Settings,
    *,
    duplicates: list[str] | None = None,
    ambiguous_date: bool = False,
    flags: list[str] | None = None,
    extraction_warnings: list[str] | None = None,
    today: date | None = None,
) -> list[Issue]:
    issues: list[Issue] = []
    today = today or date.today()
    tol, ltol = settings.amount_tolerance, settings.line_tolerance

    # 1. missing fields
    for f, sev in _MISSING_SEVERITY.items():
        if getattr(data, f) in (None, ""):
            issues.append(Issue("MISSING_FIELD", sev, f, f"{f.replace('_', ' ')} was not found"))
    if not data.line_items:
        issues.append(Issue("MISSING_FIELD", WARNING, "line_items", "no line items were found"))

    # 2. numeric sanity
    for f in ("subtotal", "tax", "total"):
        v = getattr(data, f)
        if v is not None and v < 0:
            issues.append(Issue("INVALID_AMOUNT", ERROR, f, f"{f} is negative ({_fmt(v)}); credit note or extraction error?"))
    if data.total is not None and data.total == 0:
        issues.append(Issue("INVALID_AMOUNT", WARNING, "total", "total is zero"))
    for n, li in enumerate(data.line_items, start=1):
        if li.quantity is not None and li.quantity <= 0:
            issues.append(Issue("INVALID_AMOUNT", WARNING, "line_items", f"line {n}: quantity {li.quantity} is not positive"))
        if li.amount is not None and li.amount < 0:
            issues.append(Issue("INVALID_AMOUNT", WARNING, "line_items", f"line {n}: negative amount {_fmt(li.amount)}"))
        if li.amount is None and (li.quantity is not None or li.unit_price is not None):
            issues.append(Issue("LINE_ITEM_INCOMPLETE", WARNING, "line_items", f"line {n}: amount missing"))

    # 3. arithmetic consistency
    for n, li in enumerate(data.line_items, start=1):
        if li.quantity is not None and li.unit_price is not None and li.amount is not None:
            expected = money(li.quantity * li.unit_price)
            if abs(expected - money(li.amount)) > ltol:
                issues.append(Issue(
                    "LINE_ITEM_MATH", ERROR, "line_items",
                    f"line {n}: {li.quantity} x {_fmt(li.unit_price)} = {_fmt(expected)} but amount is {_fmt(li.amount)}"))
    amounts = [li.amount for li in data.line_items]
    items_sum = sum(amounts, Decimal(0)) if amounts and all(a is not None for a in amounts) else None
    if items_sum is not None and data.subtotal is not None and abs(money(items_sum) - money(data.subtotal)) > tol:
        issues.append(Issue("SUBTOTAL_MISMATCH", ERROR, "subtotal",
                            f"line items add up to {_fmt(items_sum)} but subtotal is {_fmt(data.subtotal)}"))
    if data.total is not None:
        base = data.subtotal if data.subtotal is not None else items_sum
        if base is not None:
            expected_total = base + (data.tax or Decimal(0))
            if abs(money(expected_total) - money(data.total)) > tol:
                src = "subtotal" if data.subtotal is not None else "line items"
                tax_txt = _fmt(data.tax) if data.tax is not None else "0.00 (tax not found)"
                issues.append(Issue("TOTAL_MISMATCH", ERROR, "total",
                                    f"{src} {_fmt(base)} + tax {tax_txt} = {_fmt(expected_total)} but total is {_fmt(data.total)}"))
    if data.tax is not None and data.subtotal and data.subtotal > 0:
        rate = data.tax / data.subtotal
        if rate > Decimal("0.30"):
            issues.append(Issue("TAX_RATE_UNUSUAL", WARNING, "tax", f"tax is {rate * 100:.1f}% of subtotal"))

    # 4. dates
    if data.invoice_date:
        if data.invoice_date > today + timedelta(days=1):
            issues.append(Issue("FUTURE_DATE", WARNING, "invoice_date", f"invoice date {data.invoice_date} is in the future"))
        elif data.invoice_date < today - timedelta(days=3650):
            issues.append(Issue("OLD_DATE", WARNING, "invoice_date", f"invoice date {data.invoice_date} is over 10 years old"))
    if ambiguous_date:
        issues.append(Issue("AMBIGUOUS_DATE", WARNING, "invoice_date",
                            "day/month order is ambiguous; interpreted using the configured convention"))

    # 5. confidence + extractor flags
    for f in REQUIRED_FIELDS + ["currency"]:
        if getattr(data, f) is not None:
            m = meta.get(f)
            if m and m.confidence < settings.low_confidence and m.source != "manual":
                issues.append(Issue("LOW_CONFIDENCE", WARNING, f, f"{f} extracted with low confidence ({m.confidence:.2f})"))
    for flag in dict.fromkeys(flags or []):
        if flag == "LLM_DISAGREEMENT":
            issues.append(Issue("LLM_DISAGREEMENT", WARNING, None, "rule-based and LLM readings disagree: " +
                                "; ".join(w for w in (extraction_warnings or []) if "LLM read" in w)[:300]))
    for w in extraction_warnings or []:
        if "differs between rows" in w:
            issues.append(Issue("SOURCE_INCONSISTENT", WARNING, None, w))

    # 6. duplicates
    if duplicates:
        issues.append(Issue("DUPLICATE_INVOICE", ERROR, "invoice_number",
                            f"same supplier and invoice number already exists ({len(duplicates)} other record(s): "
                            f"{', '.join(duplicates[:3])})"))
    return issues


def needs_review(issues: list[Issue]) -> bool:
    return any(i.severity == ERROR or i.code in REVIEW_WARNING_CODES for i in issues)


def build_result_summary(res: ExtractionResult) -> dict:
    return res.data.model_dump(mode="json")
