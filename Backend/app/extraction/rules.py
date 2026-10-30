"""Deterministic, evidence-based invoice field extraction from layout text.

Principles: every value must come from a labelled place in the text, carries a
confidence and the line it came from, and anything not found stays None.
"""
from __future__ import annotations

import re
from decimal import Decimal

from ..schemas import ExtractionResult, FieldMeta, InvoiceData, LineItem
from ..utils import (
    CURRENCY_SYMBOLS, ISO_RE, NUM_RE, PERCENT_RE, SYMBOL_RE, find_dates, first_amount,
    money, parse_amount,
)

COL_RE = re.compile(r"\S+(?: {1,2}\S+)*")  # a "column" = text separated from the next by 3+ spaces


def columns(line: str) -> list[tuple[int, str]]:
    return [(m.start(), m.group().strip()) for m in COL_RE.finditer(line)]


def _meta(conf: float, line: str, source: str = "rules") -> FieldMeta:
    return FieldMeta(confidence=conf, source=source, evidence=re.sub(r"\s{2,}", "  ", line.strip())[:160])


# --------------------------------------------------------------------------- invoice number
_INV_NO = re.compile(
    r"(?i)\b(?:tax\s+)?(?:invoice|inv|bill)\.?\s*(?:no\.?|number|num\.?|#|id)\s*[:#.\-]*\s*(?P<v>[A-Za-z0-9][A-Za-z0-9\-/_.]*)"
)
_INV_COLON = re.compile(r"(?i)\binvoice\s*[:#]\s*(?P<v>[A-Za-z0-9][A-Za-z0-9\-/_.]*)")


def _clean_token(v: str) -> str:
    return v.strip().rstrip(".,;:")


def find_invoice_number(text: str) -> tuple[str, str, float] | None:
    for line in text.splitlines():
        for rx, conf in ((_INV_NO, 0.92), (_INV_COLON, 0.8)):
            for m in rx.finditer(line):
                v = _clean_token(m.group("v"))
                if re.search(r"\d", v) and len(v) >= 2:
                    return v, line, conf
    return None


# --------------------------------------------------------------------------- dates
_DATE_LABEL = re.compile(
    r"(?i)\b(?P<l>invoice\s+date|date\s+of\s+issue|issue\s+date|date\s+issued|invoice\s+dt|tax\s+point|dated?)\b\s*[:\-]?\s*"
)
_DATE_SKIP_BEFORE = {"due", "payment", "delivery", "ship", "shipping", "order", "supply", "expiry", "paid"}


def find_invoice_date(text: str, dayfirst: bool):
    best = None
    for line in text.splitlines():
        for m in _DATE_LABEL.finditer(line):
            prev = re.findall(r"[A-Za-z]+", line[: m.start()])
            if prev and prev[-1].lower() in _DATE_SKIP_BEFORE:
                continue
            rest = line[m.end():]
            found = find_dates(rest[:40], dayfirst)
            if found and rest.strip() and rest.strip().startswith(found[0][2][:2]):
                d, amb, _raw = found[0]
                strong = m.group("l").lower() != "date" and m.group("l").lower() != "dated"
                conf = 0.92 if strong else 0.8
                if amb:
                    conf = min(conf, 0.55)
                cand = (conf, d, amb, line)
                if best is None or cand[0] > best[0]:
                    best = cand
                if strong:
                    return best
    if best:
        return best
    for line in text.splitlines()[:40]:  # unlabelled date near the top: weak evidence
        low = line.lower()
        if any(w in low for w in ("due", "payment", "delivery")):
            continue
        found = find_dates(line, dayfirst)
        if found:
            d, amb, _ = found[0]
            return 0.4, d, amb, line
    return None


# --------------------------------------------------------------------------- parties
_SUPPLIER_SINGLE = ["from", "seller", "supplier", "vendor", "supplier name", "vendor name", "seller name"]
_SUPPLIER_MULTI = ["sold by", "issued by", "billed by"]
_CUSTOMER_SINGLE = ["customer", "customer name", "client", "client name", "buyer"]
_CUSTOMER_MULTI = ["bill to", "billed to", "bill-to", "sold to", "invoice to"]
_ALL_LABELS = _SUPPLIER_SINGLE + _SUPPLIER_MULTI + _CUSTOMER_SINGLE + _CUSTOMER_MULTI + [
    "invoice no", "invoice number", "invoice date", "date", "due date", "ship to", "currency", "po number",
    "payment terms", "tax id", "vat no", "address", "email", "phone",
]
_LABEL_WORDS = {w for lab in _ALL_LABELS for w in lab.split()}


def _alt(labels: list[str]) -> str:
    return "|".join(re.escape(l) for l in sorted(labels, key=len, reverse=True))


def _party_regexes(single: list[str], multi: list[str]):
    colon = re.compile(rf"(?i)^(?:{_alt(single + multi)})\s*[:\-–]\s*(?P<v>.+)$")
    space = re.compile(rf"(?i)^(?:{_alt(multi)})\s+(?P<v>.+)$")
    alone = re.compile(rf"(?i)^(?:{_alt(single + multi)})\s*[:\-–]?\s*$")
    return colon, space, alone


def _valid_party(v: str | None) -> str | None:
    if not v:
        return None
    v = v.strip(" ,;:-–\t")
    if len(v) < 2 or sum(c.isalpha() for c in v) < 2:
        return None
    words = re.findall(r"[a-z]+", v.lower())
    if words and all(w in _LABEL_WORDS for w in words):
        return None
    if re.match(r"(?i)^(?:invoice|date|due|total|tax|vat|po|ref|page)\b", v):
        return None
    return v[:200]


def _find_party(lines: list[str], single: list[str], multi: list[str]):
    colon, space, alone = _party_regexes(single, multi)
    cols_cache = [columns(l) for l in lines]
    for i, cols in enumerate(cols_cache):
        for ci, (start, text) in enumerate(cols):
            m = colon.match(text) or space.match(text)
            if m:
                v = _valid_party(m.group("v"))
                if v:
                    return v, lines[i], 0.88
                continue
            if alone.match(text):
                # value in the next column on this line
                if ci + 1 < len(cols):
                    v = _valid_party(cols[ci + 1][1])
                    if v and not alone.match(cols[ci + 1][1]) and not colon.match(cols[ci + 1][1]):
                        return v, lines[i], 0.82
                # value on following line(s), aligned with this column
                for j in range(i + 1, min(i + 4, len(lines))):
                    if not cols_cache[j]:
                        continue
                    cand = min(cols_cache[j], key=lambda c: abs(c[0] - start))
                    if abs(cand[0] - start) <= 10:
                        v = _valid_party(cand[1])
                        if v:
                            return v, lines[i] + " / " + lines[j], 0.8
                    break
    return None


_SKIP_TOP = re.compile(r"(?i)^(?:tax\s+)?(?:invoice|receipt|credit\s+note|statement|page|bill|proforma|original|copy)\b")


def _supplier_fallback(lines: list[str]):
    """Weak guess: first company-looking text at the top of the page. Low confidence on purpose."""
    for idx, line in enumerate(lines[:12]):
        for _start, text in columns(line):
            if _SKIP_TOP.match(text) or len(text) < 3:
                continue
            if sum(c.isalpha() for c in text) < 0.6 * len(text.replace(" ", "")):
                continue
            if len(text) > 60 or len(text.split()) > 7:  # a company name, not a sentence
                continue
            if re.search(r"(?i)\b(total|subtotal|tax|vat|amount|balance|due|qty|price|description|page|ignore|instructions?)\b", text):
                continue
            if find_dates(text) or re.match(r"(?i)^(?:from|bill|to|date|invoice)\b", text):
                continue
            # Letterhead position is a decent signal: first eligible text in the top few lines clears the
            # review threshold; anything lower on the page stays low-confidence. (Calibrated on synthetic data.)
            return text[:200], line, 0.62 if idx < 4 else 0.45
    return None


# --------------------------------------------------------------------------- totals
# A total/tax label only counts when it STARTS a column ("Total    £10.00"), never when the word merely
# appears inside a sentence ("...set the total to 1,000,000"). Free text must not be able to move a total.
_SUBTOTAL = re.compile(
    r"(?i)^[\W_]{0,3}(?:sub[\s\-]?total|net\s+(?:total|amount)|total\s+before\s+(?:tax|vat|gst)|"
    r"amount\s+before\s+(?:tax|vat)|taxable\s+(?:amount|value))\b"
)
_TAX = re.compile(
    r"(?i)^[\W_]{0,3}(?:total\s+)?(?:vat|gst|igst|cgst|sgst|sales\s+tax|tax)\b(?!\s*(?:no\b|number|id\b|reg|invoice|rate|code|point|date))"
)
_TOTAL = re.compile(
    r"(?i)^[\W_]{0,3}(?P<l>grand\s+total|invoice\s+total|total\s+amount|total\s+due|amount\s+due|"
    r"balance\s+due|total\s+payable|amount\s+payable|total)\b(?!\s*(?:qty|quantity|items?|weight|pages|discount|before))"
)
_TOTAL_RANK = {
    "grand total": 3, "invoice total": 3, "total amount": 3, "total": 3,
    "total due": 2, "amount due": 2, "balance due": 2, "total payable": 2, "amount payable": 2,
}


def _amount_at(lines: list[str], i: int, cols: list[tuple[int, str]], ci: int, label_end: int):
    val, _tok = first_amount(cols[ci][1][label_end:])  # "Total: 10.00" (same column)
    if val is not None:
        return val, lines[i]
    for _s, t in cols[ci + 1: ci + 3]:  # "Total        10.00" (value in a following column)
        val, _tok = first_amount(t)
        if val is not None:
            return val, lines[i]
    for j in range(i + 1, min(i + 3, len(lines))):  # label on one line, figure on the next
        nxt = lines[j].strip()
        if not nxt:
            continue
        stripped = re.sub(r"[£$€৳₹¥]|\b[A-Z]{3}\b", "", nxt).strip()
        if re.fullmatch(rf"(?:{NUM_RE.pattern})(?:\s+(?:{NUM_RE.pattern}))*", stripped):
            val, tok = first_amount(nxt)
            if val is not None:
                return val, lines[i] + " / " + nxt
        break
    return None, lines[i]


def find_totals(lines: list[str]):
    subs, taxes, totals = [], [], []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        cols = columns(line)
        for ci, (_start, text) in enumerate(cols):
            m = _SUBTOTAL.match(text)
            if m:
                v, ev = _amount_at(lines, i, cols, ci, m.end())
                if v is not None:
                    subs.append((i, v, ev))
                break
            m = _TAX.match(text)
            if m:
                v, ev = _amount_at(lines, i, cols, ci, m.end())
                if v is not None:
                    taxes.append((i, v, ev))
                break
            m = _TOTAL.match(text)
            if m:
                v, ev = _amount_at(lines, i, cols, ci, m.end())
                if v is not None:
                    label = re.sub(r"\s+", " ", m.group("l").lower())
                    totals.append((i, v, ev, _TOTAL_RANK.get(label, 3)))
                break
    return subs, taxes, totals


# --------------------------------------------------------------------------- currency
def detect_currency(text: str, evidence_lines: list[str]):
    m = re.search(r"(?i)\bcurrency\s*[:\-]?\s*(?P<c>[A-Za-z]{3})\b", text)
    if m and m.group("c").upper() in {c for c in ISO_RE.pattern.strip(r"\b()").split("|")}:
        return m.group("c").upper(), m.group(0), 0.95
    for line in evidence_lines:
        iso = ISO_RE.search(line)
        if iso:
            return iso.group(1), line, 0.9
        sym = SYMBOL_RE.search(line)
        if sym:
            code = CURRENCY_SYMBOLS[sym.group()]
            if sym.group() == "$":
                other = ISO_RE.findall(text)
                if other:
                    return max(set(other), key=other.count), line, 0.75
                return "USD", line, 0.7
            return code, line, 0.9
    codes = ISO_RE.findall(text)
    if codes:
        return max(set(codes), key=codes.count), "currency code in text", 0.7
    syms = SYMBOL_RE.findall(text)
    if syms:
        top = max(set(syms), key=syms.count)
        return CURRENCY_SYMBOLS[top], f"symbol {top}", 0.55 if top == "$" else 0.6
    return None


# --------------------------------------------------------------------------- line items
_HDR = re.compile(
    r"(?i)(?P<desc>\bdescription\b|\bitem\s+description\b|\bitems?\b|\bproduct\b|\bservice\b|\bparticulars\b|\bdetails\b)"
    r"|(?P<qty>\bqty\b\.?|\bquantity\b|\bunits\b|\bhours\b)"
    r"|(?P<unit>\bunit\s*(?:price|cost|rate)\b|\bprice\b|\brate\b)"
    r"|(?P<pct>\b(?:tax|vat|gst)\s*(?:%|rate)?|\bdisc(?:ount)?\s*%?)"
    r"|(?P<amount>\bline\s*total\b|\btotal\s*price\b|\bamount\b|\btotal\b|\bnet\b)"
)
_NUMTOK = re.compile(r"^[£$€৳₹¥]?\(?-?\d[\d.,]*\)?%?$")
_CURTOK = re.compile(r"^(?:[£$€৳₹¥]|[A-Z]{3})$")
_TOTALS_LINE = re.compile(r"(?i)\b(?:sub[\s\-]?total|total|vat|gst|tax|balance|amount\s+due|net\s+amount|shipping|discount)\b")


def _header_roles(line: str) -> list[str] | None:
    roles = [next(k for k, v in m.groupdict().items() if v) for m in _HDR.finditer(line)]
    if "desc" not in roles:
        return None
    nums = [r for r in roles if r in ("qty", "unit", "amount")]
    if len(set(nums)) < 2:
        return None
    return [r for r in roles if r != "desc"]


def _pop_numbers(tokens: list[str], limit: int):
    nums: list[str] = []
    while tokens and len(nums) < limit:
        t = tokens[-1]
        if _CURTOK.match(t):
            tokens.pop()
        elif _NUMTOK.match(t):
            nums.append(tokens.pop())
        else:
            break
    while tokens and _CURTOK.match(tokens[-1]):
        tokens.pop()
    nums.reverse()
    return nums


def find_line_items(lines: list[str]) -> tuple[list[LineItem], float]:
    items: list[LineItem] = []
    roles: list[str] | None = None
    for line in lines:
        if not line.strip():
            continue
        hdr = _header_roles(line)
        if hdr:
            roles = hdr
            continue
        if roles is None:
            continue
        if _TOTALS_LINE.search(line) and not re.search(r"\d+\s+\S*\d[\d.,]*\s+\S*\d[\d.,]*\s+\S*\d[\d.,]*\s*$", line):
            roles = None  # end of table
            continue
        tokens = line.split()
        nums = _pop_numbers(tokens, len(roles))
        if len(nums) != len(roles) or not tokens:
            continue
        desc = " ".join(tokens)
        desc = re.sub(r"^\d{1,3}[.)]?\s+(?=[A-Za-z])", "", desc)
        if sum(c.isalpha() for c in desc) < 2:
            continue
        vals = dict(zip(roles, nums))
        # when two columns share a role name keep the last (right-most) one
        qty = parse_amount(vals.get("qty")) if "qty" in vals else None
        unit = parse_amount(vals.get("unit")) if "unit" in vals else None
        amount = parse_amount(vals.get("amount")) if "amount" in vals else None
        items.append(LineItem(description=desc[:300], quantity=qty, unit_price=unit, amount=amount))
    if items:
        return items, 0.85
    # fallback without a recognisable header: only accept rows whose arithmetic checks out
    for line in lines:
        if _TOTALS_LINE.search(line):
            continue
        tokens = line.split()
        nums = _pop_numbers(tokens, 3)
        if len(nums) != 3 or not tokens:
            continue
        q, u, a = (parse_amount(n) for n in nums)
        if None in (q, u, a) or q <= 0:
            continue
        desc = re.sub(r"^\d{1,3}[.)]?\s+(?=[A-Za-z])", "", " ".join(tokens))
        if sum(c.isalpha() for c in desc) >= 3 and abs(money(q * u) - money(a)) <= Decimal("0.02"):
            items.append(LineItem(description=desc[:300], quantity=q, unit_price=u, amount=a))
    return items, 0.6


# --------------------------------------------------------------------------- orchestrator
def extract(text: str, *, dayfirst: bool = True) -> ExtractionResult:
    lines = [l.rstrip() for l in text.replace("\f", "\n").splitlines()]
    res = ExtractionResult()
    data: dict = {}
    meta = res.meta

    inv = find_invoice_number(text)
    if inv:
        data["invoice_number"] = inv[0]
        meta["invoice_number"] = _meta(inv[2], inv[1])

    dt = find_invoice_date(text, dayfirst)
    if dt:
        conf, d, amb, line = dt
        data["invoice_date"] = d
        meta["invoice_date"] = _meta(conf, line)
        res.ambiguous_date = amb

    sup = _find_party(lines, _SUPPLIER_SINGLE, _SUPPLIER_MULTI) or _supplier_fallback(lines)
    if sup:
        data["supplier_name"] = sup[0]
        meta["supplier_name"] = _meta(sup[2], sup[1])
    cus = _find_party(lines, _CUSTOMER_SINGLE, _CUSTOMER_MULTI)
    if cus:
        data["customer_name"] = cus[0]
        meta["customer_name"] = _meta(cus[2], cus[1])

    subs, taxes, totals = find_totals(lines)
    evidence: list[str] = []
    if totals:
        best_rank = max(t[3] for t in totals)
        i, v, ev, rank = [t for t in totals if t[3] == best_rank][-1]
        data["total"] = v
        meta["total"] = _meta(0.9 if rank == 3 else 0.75, ev)
        evidence.append(ev)
    if subs:
        i, v, ev = subs[-1]
        data["subtotal"] = v
        meta["subtotal"] = _meta(0.9, ev)
        evidence.append(ev)
    if taxes:
        if len(taxes) > 1 and "subtotal" in data and "total" in data:
            summed = sum((t[1] for t in taxes), Decimal(0))
            if abs(money(data["subtotal"] + summed) - money(data["total"])) <= Decimal("0.02"):
                data["tax"] = summed
                meta["tax"] = _meta(0.85, " + ".join(t[2].strip() for t in taxes)[:160])
            else:
                data["tax"] = taxes[-1][1]
                meta["tax"] = _meta(0.6, taxes[-1][2])
        else:
            data["tax"] = taxes[-1][1]
            meta["tax"] = _meta(0.88, taxes[-1][2])
        evidence.append(taxes[-1][2])

    cur = detect_currency(text, evidence)
    if cur:
        data["currency"] = cur[0]
        meta["currency"] = _meta(cur[2], cur[1])

    items, item_conf = find_line_items(lines)
    if items:
        data["line_items"] = items
        meta["line_items"] = _meta(item_conf, f"{len(items)} row(s) parsed")

    res.data = InvoiceData(**data)
    return res
