"""Validation rules: missing fields, numeric errors, duplicates, subtotal/tax/line-item/total consistency."""
from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from app.config import Settings
from app.schemas import FieldMeta, InvoiceData, LineItem
from app.validation import needs_review, validate

TODAY = date(2026, 10, 1)
S = Settings()


def good(**over) -> InvoiceData:
    base = dict(
        invoice_number="INV-1", invoice_date=date(2026, 9, 1), supplier_name="Acme Ltd", customer_name="Beta Plc",
        subtotal=D("30.00"), tax=D("6.00"), total=D("36.00"), currency="GBP",
        line_items=[LineItem(description="A", quantity=D(2), unit_price=D("5.00"), amount=D("10.00")),
                    LineItem(description="B", quantity=D(1), unit_price=D("20.00"), amount=D("20.00"))],
    )
    base.update(over)
    return InvoiceData(**base)


def meta(conf=0.9) -> dict:
    return {f: FieldMeta(confidence=conf) for f in ("invoice_number", "invoice_date", "supplier_name", "total", "currency")}


def codes(data, **kw) -> set[str]:
    return {i.code for i in validate(data, kw.pop("meta", meta()), S, today=TODAY, **kw)}


def test_clean_invoice_has_no_issues_and_needs_no_review():
    issues = validate(good(), meta(), S, today=TODAY)
    assert issues == [] and not needs_review(issues)


@pytest.mark.parametrize("field,severity", [
    ("invoice_number", "error"), ("invoice_date", "error"), ("supplier_name", "error"), ("total", "error"),
    ("customer_name", "warning"), ("currency", "warning"), ("subtotal", "warning"), ("tax", "warning"),
])
def test_missing_fields(field, severity):
    issues = validate(good(**{field: None}), meta(), S, today=TODAY)
    miss = [i for i in issues if i.code == "MISSING_FIELD" and i.field == field]
    assert miss and miss[0].severity == severity
    assert needs_review(issues)


def test_missing_line_items_is_flagged():
    assert "MISSING_FIELD" in codes(good(line_items=[]))


def test_negative_and_zero_amounts():
    assert "INVALID_AMOUNT" in codes(good(total=D("-36.00")))
    assert "INVALID_AMOUNT" in codes(good(tax=D("-6.00")))
    assert "INVALID_AMOUNT" in codes(good(subtotal=D("0"), tax=D("0"), total=D("0"), line_items=[]))
    li = [LineItem(description="x", quantity=D(0), unit_price=D("5"), amount=D("0"))]
    assert "INVALID_AMOUNT" in codes(good(line_items=li, subtotal=D("0"), tax=D("0"), total=D("0")))


def test_line_item_arithmetic_error():
    li = [LineItem(description="A", quantity=D(2), unit_price=D("5.00"), amount=D("11.00")),
          LineItem(description="B", quantity=D(1), unit_price=D("20.00"), amount=D("20.00"))]
    issues = validate(good(line_items=li, subtotal=D("31.00"), tax=D("6.20"), total=D("37.20")), meta(), S, today=TODAY)
    msgs = [i.message for i in issues if i.code == "LINE_ITEM_MATH"]
    assert len(msgs) == 1 and "line 1" in msgs[0] and "10.00" in msgs[0]


def test_line_item_rounding_within_a_cent_is_tolerated():
    li = [LineItem(description="A", quantity=D("3"), unit_price=D("3.333"), amount=D("10.00"))]  # 9.999 -> 10.00
    assert "LINE_ITEM_MATH" not in codes(good(line_items=li, subtotal=D("10.00"), tax=D("2.00"), total=D("12.00")))


def test_subtotal_vs_line_items():
    assert "SUBTOTAL_MISMATCH" in codes(good(subtotal=D("35.00"), tax=D("7.00"), total=D("42.00")))
    assert "SUBTOTAL_MISMATCH" not in codes(good())


def test_total_vs_subtotal_plus_tax():
    issues = validate(good(total=D("40.00")), meta(), S, today=TODAY)
    mm = [i for i in issues if i.code == "TOTAL_MISMATCH"]
    assert mm and "36.00" in mm[0].message and "40.00" in mm[0].message
    assert "TOTAL_MISMATCH" not in codes(good(total=D("36.02")))   # within 2p tolerance
    assert "TOTAL_MISMATCH" in codes(good(total=D("36.05")))


def test_total_check_falls_back_to_line_items_when_subtotal_missing():
    assert "TOTAL_MISMATCH" not in codes(good(subtotal=None))
    assert "TOTAL_MISMATCH" in codes(good(subtotal=None, total=D("99.00")))


def test_missing_tax_with_total_above_subtotal_is_flagged():
    issues = validate(good(tax=None), meta(), S, today=TODAY)
    assert any(i.code == "TOTAL_MISMATCH" and "tax not found" in i.message for i in issues)


def test_zero_tax_invoice_is_fine():
    d = good(tax=D("0.00"), total=D("30.00"))
    assert "TOTAL_MISMATCH" not in codes(d)


def test_unusual_tax_rate():
    assert "TAX_RATE_UNUSUAL" in codes(good(tax=D("15.00"), total=D("45.00")))


def test_duplicates():
    issues = validate(good(), meta(), S, today=TODAY, duplicates=["abc", "def"])
    dup = [i for i in issues if i.code == "DUPLICATE_INVOICE"]
    assert dup and dup[0].severity == "error" and "abc" in dup[0].message and needs_review(issues)


def test_dates():
    assert "FUTURE_DATE" in codes(good(invoice_date=date(2027, 1, 1)))
    assert "OLD_DATE" in codes(good(invoice_date=date(2010, 1, 1)))
    assert "AMBIGUOUS_DATE" in codes(good(), ambiguous_date=True)


def test_low_confidence_triggers_review_but_manual_values_do_not():
    low = meta(0.4)
    issues = validate(good(), low, S, today=TODAY)
    assert {"LOW_CONFIDENCE"} == {i.code for i in issues} and needs_review(issues)
    manual = {f: FieldMeta(confidence=1.0, source="manual") for f in low}
    assert validate(good(), manual, S, today=TODAY) == []
    only_manual_low = {f: FieldMeta(confidence=0.1, source="manual") for f in low}
    assert validate(good(), only_manual_low, S, today=TODAY) == []


def test_llm_disagreement_and_source_inconsistency_are_review_grade():
    issues = validate(good(), meta(), S, today=TODAY, flags=["LLM_DISAGREEMENT"],
                      extraction_warnings=["total: rules read '1', LLM read '2'", "total differs between rows of the same invoice"])
    assert {"LLM_DISAGREEMENT", "SOURCE_INCONSISTENT"} <= {i.code for i in issues}
    assert needs_review(issues)


def test_warning_only_issues_do_not_force_review():
    issues = validate(good(), meta(), S, today=TODAY)  # none
    from app.validation import Issue
    assert not needs_review([Issue("FUTURE_DATE", "warning", "invoice_date", "x"), Issue("OLD_DATE", "warning", None, "y")])
    assert issues == []


def test_each_issue_names_a_field_for_ui_highlighting():
    issues = validate(good(total=D("99")), meta(), S, today=TODAY)
    assert all(i.field for i in issues if i.code in {"TOTAL_MISMATCH", "MISSING_FIELD", "SUBTOTAL_MISMATCH"})
