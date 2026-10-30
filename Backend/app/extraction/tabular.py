"""Invoice extraction from spreadsheet grids (XLSX / XLS / CSV).

Two layouts are recognised:
  * register  - a table with an invoice-number column; rows are grouped into invoices
  * single    - one invoice laid out like a printed page: key/value cells + a line-item table
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal
from typing import Any

from ..loaders.base import Sheet
from ..loaders.spreadsheet import fmt_cell, sheet_to_text
from ..schemas import ExtractionResult, FieldMeta, InvoiceData, LineItem
from ..utils import find_dates, parse_amount
from . import rules

ROLE_SYNONYMS: dict[str, set[str]] = {
    "invoice_number": {"invoice number", "invoice no", "invoice #", "inv no", "inv number", "inv #", "invoice id",
                       "invoice", "bill no", "document number", "doc no"},
    "invoice_date": {"invoice date", "date", "issue date", "date of issue", "inv date", "billing date"},
    "supplier_name": {"supplier", "supplier name", "vendor", "vendor name", "seller", "from", "issuer"},
    "customer_name": {"customer", "customer name", "client", "client name", "bill to", "buyer", "sold to"},
    "currency": {"currency", "curr", "ccy"},
    "description": {"description", "item", "item description", "product", "details", "particulars", "service", "line item"},
    "quantity": {"qty", "quantity", "units", "hours"},
    "unit_price": {"unit price", "price", "rate", "unit cost", "unit rate"},
    "amount": {"amount", "line total", "line amount", "total price", "net amount", "value", "line value"},
    "subtotal": {"subtotal", "sub total", "net total", "net", "invoice subtotal"},
    "tax": {"tax", "vat", "gst", "tax amount", "vat amount", "sales tax"},
    "total": {"total", "grand total", "invoice total", "total amount", "amount due", "gross total"},
}
_SYN_LOOKUP = {syn: role for role, syns in ROLE_SYNONYMS.items() for syn in syns}


def _norm_header(v: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9# ]+", " ", fmt_cell(v).lower())).strip()


def _row_roles(row: list[Any]) -> dict[int, str]:
    out: dict[int, str] = {}
    seen: set[str] = set()
    for j, c in enumerate(row):
        role = _SYN_LOOKUP.get(_norm_header(c))
        if role and role not in seen:
            out[j] = role
            seen.add(role)
    return out


def _find_header(rows: list[list[Any]]) -> tuple[int, dict[int, str]] | None:
    for i, row in enumerate(rows[:60]):
        roles = _row_roles(row)
        names = set(roles.values())
        if len(names) >= 3 and ("description" in names or "invoice_number" in names):
            return i, roles
    return None


def _cell(row: list[Any], j: int) -> Any:
    return row[j] if j < len(row) else None


def _is_totals_row(row: list[Any]) -> bool:
    first = next((fmt_cell(c) for c in row if fmt_cell(c)), "")
    return bool(re.match(r"(?i)^(sub[\s\-]?total|total|vat|gst|tax|balance|amount due|grand total|net)\b", first))


def _parse_date(v: Any, dayfirst: bool):
    s = fmt_cell(v)
    found = find_dates(s, dayfirst)
    if found:
        d, amb, _ = found[0]
        return d, amb
    return None, False


def _meta(conf: float, ev: str) -> FieldMeta:
    return FieldMeta(confidence=conf, source="sheet", evidence=ev[:160])


def _line_item(rowvals: dict[str, Any]) -> LineItem | None:
    desc = fmt_cell(rowvals.get("description")) or None
    qty = parse_amount(rowvals.get("quantity"))
    unit = parse_amount(rowvals.get("unit_price"))
    amt = parse_amount(rowvals.get("amount"))
    if desc is None and qty is None and unit is None and amt is None:
        return None
    return LineItem(description=desc[:300] if desc else None, quantity=qty, unit_price=unit, amount=amt)


def _reinterpret_total(roles: dict[int, str]) -> dict[int, str]:
    """A lone 'Total' column next to qty/unit price is the line total, not the invoice total."""
    names = set(roles.values())
    if "total" in names and "amount" not in names and ({"quantity", "unit_price"} & names) and "subtotal" not in names:
        return {j: ("amount" if r == "total" else r) for j, r in roles.items()}
    return roles


def _extract_register(rows, header_i, roles, dayfirst) -> list[ExtractionResult]:
    roles = _reinterpret_total(roles)
    groups: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    current: str | None = None
    unnumbered = 0
    for row in rows[header_i + 1:]:
        if not any(fmt_cell(c) for c in row) or _is_totals_row(row):
            continue
        vals = {role: _cell(row, j) for j, role in roles.items()}
        key = fmt_cell(vals.get("invoice_number"))
        has_line = any(fmt_cell(vals.get(k)) for k in ("description", "amount", "quantity", "unit_price"))
        if not key and has_line and current:
            # blank cell: usually a merged/continued invoice-number cell, so forward-fill. But if this row
            # carries different invoice-level facts (supplier / customer / date) it is a different invoice
            # that simply has no number - keep it separate rather than merge two invoices together.
            first = groups[current][0]
            differs = any(
                fmt_cell(vals.get(r)) and fmt_cell(first.get(r)) and fmt_cell(vals.get(r)) != fmt_cell(first.get(r))
                for r in ("supplier_name", "customer_name", "invoice_date")
            )
            if differs:
                unnumbered += 1
                key = f"\0unnumbered-{unnumbered}"
            else:
                key = current
        if not key:
            continue
        current = key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(vals)

    results = []
    for key in order:
        recs = groups[key]
        res = ExtractionResult()
        data: dict[str, Any] = {}
        if not key.startswith("\0"):
            data["invoice_number"] = key
            res.meta["invoice_number"] = _meta(0.95, f"column 'invoice number' = {key}")

        def most_common(role: str) -> tuple[Any, bool]:
            vals = [v for v in (_cell_vals(r, role) for r in recs) if v not in (None, "")]
            if not vals:
                return None, False
            c = Counter(fmt_cell(v) for v in vals)
            top, _n = c.most_common(1)[0]
            first = next(v for v in vals if fmt_cell(v) == top)
            return first, len(c) > 1

        for role in ("supplier_name", "customer_name"):
            v, conflict = most_common(role)
            if v is not None:
                data[role] = fmt_cell(v)[:200]
                res.meta[role] = _meta(0.5 if conflict else 0.95, f"column '{role}'")
        v, conflict = most_common("currency")
        if v is not None:
            cur = fmt_cell(v).upper()
            if re.fullmatch(r"[A-Z]{3}", cur):
                data["currency"] = cur
                res.meta["currency"] = _meta(0.5 if conflict else 0.95, "column 'currency'")
        v, conflict = most_common("invoice_date")
        if v is not None:
            d, amb = _parse_date(v, dayfirst)
            if d:
                data["invoice_date"] = d
                res.meta["invoice_date"] = _meta(0.55 if (amb or conflict) else 0.95, "column 'invoice date'")
                res.ambiguous_date = amb
        for role in ("subtotal", "tax", "total"):
            v, conflict = most_common(role)
            amt = parse_amount(v)
            if amt is not None:
                data[role] = amt
                res.meta[role] = _meta(0.5 if conflict else 0.95, f"column '{role}'")
                if conflict:
                    res.warnings.append(f"{role} differs between rows of the same invoice")
        items = [li for li in (_line_item(r) for r in recs) if li]
        if items:
            data["line_items"] = items
            res.meta["line_items"] = _meta(0.95, f"{len(items)} row(s)")
        res.data = InvoiceData(**data)
        results.append(res)
    return results


def _cell_vals(rec: dict[str, Any], role: str) -> Any:
    return rec.get(role)


def _extract_single(sheet: Sheet, rows, header_i, roles, dayfirst) -> ExtractionResult:
    roles = _reinterpret_total(roles)
    text = sheet_to_text(rows)
    res = rules.extract(text, dayfirst=dayfirst)
    for m in res.meta.values():
        m.source = "sheet"
    items: list[LineItem] = []
    blank_run = 0
    for row in rows[header_i + 1:]:
        if not any(fmt_cell(c) for c in row):
            blank_run += 1
            if items and blank_run >= 2:
                break
            continue
        blank_run = 0
        if _is_totals_row(row):
            break
        li = _line_item({role: _cell(row, j) for j, role in roles.items()})
        if li:
            items.append(li)
    if items:
        res.data.line_items = items
        res.meta["line_items"] = _meta(0.95, f"{len(items)} row(s)")
    return res


def extract_sheets(sheets: list[Sheet], *, dayfirst: bool = True) -> list[tuple[ExtractionResult, str, str]]:
    """Return [(result, sheet_text, sheet_name)] for every sheet that looks like it holds invoice data."""
    out: list[tuple[ExtractionResult, str, str]] = []
    for sh in sheets:
        rows = sh.rows
        if not rows:
            continue
        text = sheet_to_text(rows)
        hdr = _find_header(rows)
        if hdr:
            i, roles = hdr
            if "invoice_number" in roles.values():
                for res in _extract_register(rows, i, roles, dayfirst):
                    out.append((res, text, sh.name))
                continue
            res = _extract_single(sh, rows, i, roles, dayfirst)
            out.append((res, text, sh.name))
            continue
        res = rules.extract(text, dayfirst=dayfirst)
        for m in res.meta.values():
            m.source = "sheet"
        if res.data.invoice_number or res.data.total is not None or res.data.line_items:
            out.append((res, text, sh.name))
    return out
