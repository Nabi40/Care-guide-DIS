from datetime import date
from decimal import Decimal

import pytest

from app.utils import DATE_FIND, find_dates, first_amount, normalize_name, normalize_number, parse_amount, parse_date_match


@pytest.mark.parametrize("raw,expected", [
    ("1,234.50", "1234.50"), ("£1,234.50", "1234.50"), ("1.234,50", "1234.50"), ("1.234,50 €", "1234.50"),
    ("12,50", "12.50"), ("1,234", "1234"), ("1234.5", "1234.5"), ("(12.00)", "-12.00"), ("-3.2", "-3.2"),
    ("$ 100", "100"), ("USD 2,000.00", "2000.00"), ("1,234,567.89", "1234567.89"), ("0.99", "0.99"),
    (7, "7"), (Decimal("3.10"), "3.10"), (2.5, "2.5"),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["", None, "abc", "N/A", "-", True])
def test_parse_amount_rejects_non_numbers(raw):
    assert parse_amount(raw) is None


def test_first_amount_prefers_money_over_years_and_ignores_percent():
    assert first_amount(" (20%)   £1,200.00 due by 15 Nov 2026")[0] == Decimal("1200.00")
    assert first_amount("VAT 5% 43.25")[0] == Decimal("43.25")
    assert first_amount("no numbers here") == (None, None)


def _d(s, dayfirst=True):
    return parse_date_match(DATE_FIND.search(s), dayfirst)


@pytest.mark.parametrize("text,expected,ambiguous", [
    ("2026-03-05", date(2026, 3, 5), False),
    ("25/03/2026", date(2026, 3, 25), False),   # day > 12 is unambiguous
    ("03/25/2026", date(2026, 3, 25), False),   # month-first inferred because 25 > 12
    ("05/03/2026", date(2026, 3, 5), True),     # ambiguous -> dayfirst, flagged
    ("05/05/2026", date(2026, 5, 5), False),    # same either way
    ("5 March 2026", date(2026, 3, 5), False),
    ("March 5th, 2026", date(2026, 3, 5), False),
    ("12-Sep-2026", date(2026, 9, 12), False),  # dashed month-name form
    ("Sept 3, 2026", date(2026, 9, 3), False),
])
def test_dates(text, expected, ambiguous):
    assert _d(text) == (expected, ambiguous)


def test_dayfirst_switch_and_invalid_dates():
    assert _d("05/03/2026", dayfirst=False)[0] == date(2026, 5, 3)
    assert find_dates("31/02/2026") == []  # impossible date
    assert find_dates("01/01/1850") == []  # outside sanity window


def test_normalisation_keys():
    assert normalize_name("ACME Trading Ltd.") == normalize_name("Acme  Trading Limited") == "acme trading"
    assert normalize_name("Smith & Sons Inc") == "smith and sons"
    assert normalize_number("INV-2026/0042") == normalize_number("inv 2026 0042") == "inv20260042"
    assert normalize_name(None) == "" and normalize_number(None) == ""
