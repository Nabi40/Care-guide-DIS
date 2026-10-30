"""Parsing helpers for money, dates and names. Pure functions, no I/O."""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from dateutil import parser as dateparser

CURRENCY_SYMBOLS = {"£": "GBP", "€": "EUR", "৳": "BDT", "₹": "INR", "¥": "JPY", "$": "USD"}
ISO_CODES = {
    "USD", "GBP", "EUR", "BDT", "INR", "PKR", "AUD", "CAD", "CHF", "JPY", "AED",
    "SAR", "SGD", "CNY", "SEK", "NOK", "DKK", "NZD", "ZAR", "MYR",
}
ISO_RE = re.compile(r"\b(" + "|".join(sorted(ISO_CODES)) + r")\b")
SYMBOL_RE = re.compile("[" + "".join(CURRENCY_SYMBOLS) + "]")

# Number tokens: "1,234.50", "1.234,50", "1234.5", "(12.00)", "-3"
NUM_RE = re.compile(r"\(?-?\d{1,3}(?:[,.]\d{3})+(?:[.,]\d{1,2})?\)?|\(?-?\d+(?:[.,]\d+)?\)?")
PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*%")

CENT = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(raw: object) -> Decimal | None:
    """Parse '£1,234.50', '1.234,50', '(12.00)', '12,50' ... into a Decimal. None if not a number."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            return Decimal(str(raw))
        except InvalidOperation:
            return None
    s = str(raw).strip()
    if not s:
        return None
    neg = (
        (s.startswith("(") and s.endswith(")"))
        or bool(re.match(r"^[^\d()]*-\s*\d", s))
        or s.endswith("-")
        or bool(re.search(r"\bCR\b", s, re.I))
    )
    digits = re.sub(r"[^\d.,]", "", s).strip(".,")
    if not re.search(r"\d", digits):
        return None
    last_dot, last_com = digits.rfind("."), digits.rfind(",")
    dec: str | None
    if last_dot >= 0 and last_com >= 0:
        dec = "." if last_dot > last_com else ","
    elif last_com >= 0:
        tail = digits[last_com + 1:]
        dec = "," if digits.count(",") == 1 and len(tail) != 3 else None
    elif last_dot >= 0:
        dec = "." if digits.count(".") == 1 else None
    else:
        dec = None
    if dec:
        ip, _, fp = digits.rpartition(dec)
        num = f"{re.sub(r'[.,]', '', ip) or '0'}.{fp}"
    else:
        num = re.sub(r"[.,]", "", digits)
    try:
        value = Decimal(num)
    except InvalidOperation:
        return None
    return -value if neg else value


def first_amount(segment: str) -> tuple[Decimal | None, str | None]:
    """First money-looking number in a text segment (percentages ignored). Returns (value, raw token)."""
    segment = PERCENT_RE.sub(" ", segment)
    tokens = [m.group() for m in NUM_RE.finditer(segment)]
    if not tokens:
        return None, None
    money_like = [t for t in tokens if re.search(r"[.,]\d{2}\)?$", t)]
    token = (money_like or tokens)[0]
    return parse_amount(token), token


# --------------------------------------------------------------------------- dates
_MONTHS = (
    "January|February|March|April|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec"
)
DATE_FIND = re.compile(
    r"\b(?P<iso>\d{4}[-/.]\d{1,2}[-/.]\d{1,2})\b"
    r"|\b(?P<num>\d{1,2}[/.\-]\d{1,2}[/.\-]\d{2,4})\b"
    rf"|\b(?P<dmy>\d{{1,2}}(?:st|nd|rd|th)?[\s\-]+(?:{_MONTHS})\.?,?[\s\-]+\d{{4}})\b"
    rf"|\b(?P<mdy>(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}})\b",
    re.I,
)


def _valid(y: int, m: int, d: int) -> date | None:
    if not (1990 <= y <= 2100):
        return None
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date_match(m: re.Match[str], dayfirst: bool = True) -> tuple[date | None, bool]:
    """Return (date, ambiguous). ambiguous=True when dd/mm vs mm/dd cannot be told apart."""
    if m.group("iso"):
        y, mo, d = (int(x) for x in re.split(r"[-/.]", m.group("iso")))
        return _valid(y, mo, d), False
    if m.group("num"):
        a, b, c = (int(x) for x in re.split(r"[/.\-]", m.group("num")))
        y = c + 2000 if c < 100 else c
        if a > 12 >= b:
            return _valid(y, b, a), False
        if b > 12 >= a:
            return _valid(y, a, b), False
        if a <= 12 and b <= 12:
            day, month = (a, b) if dayfirst else (b, a)
            return _valid(y, month, day), a != b
        return None, False
    text = m.group("dmy") or m.group("mdy") or ""
    text = re.sub(r"(?<=\d)(st|nd|rd|th)", "", text, flags=re.I)
    try:
        parsed = dateparser.parse(text, dayfirst=True, fuzzy=False)
    except (ValueError, OverflowError):
        return None, False
    return _valid(parsed.year, parsed.month, parsed.day), False


def find_dates(text: str, dayfirst: bool = True) -> list[tuple[date, bool, str]]:
    out = []
    for m in DATE_FIND.finditer(text):
        d, amb = parse_date_match(m, dayfirst)
        if d:
            out.append((d, amb, m.group()))
    return out


# --------------------------------------------------------------------------- names / keys
_LEGAL = {
    "ltd", "limited", "inc", "incorporated", "llc", "llp", "plc", "corp", "corporation",
    "co", "company", "pvt", "private", "pte", "gmbh", "sa", "bv", "ag",
}


def normalize_name(name: str | None) -> str:
    if not name:
        return ""
    s = name.casefold().replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    words = [w for w in s.split() if w not in _LEGAL]
    return " ".join(words)


def normalize_number(num: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (num or "").casefold())


def norm_text(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.casefold()).strip()
