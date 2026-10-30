"""Optional LLM extraction with a grounding check.

The LLM may only fill gaps; every value it returns must be found in the source text,
otherwise it is dropped. This is what prevents invented values.
"""
from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation
from datetime import date
from typing import Protocol

import httpx

from ..schemas import ExtractionResult, FieldMeta, InvoiceData, LineItem
from ..utils import NUM_RE, find_dates, money, norm_text, parse_amount
from .prompts import INVOICE_SCHEMA, SYSTEM_PROMPT, USER_TEMPLATE

log = logging.getLogger(__name__)
AMOUNT_FIELDS = ("subtotal", "tax", "total")


class LLMClient(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class LLMResponseError(RuntimeError):
    """The model answered, but not with a usable extraction (truncated output)."""


class OllamaClient:
    """Local model served by Ollama: free, no API key, and document text stays on this machine."""

    def __init__(self, model: str, base_url: str = "http://127.0.0.1:11434", timeout: int = 300,
                 num_ctx: int = 16384, http_client: httpx.Client | None = None):
        self.model, self.num_ctx = model, num_ctx
        self._url = base_url.rstrip("/") + "/api/chat"
        self._client = http_client or httpx.Client(timeout=timeout)

    def complete(self, system: str, user: str) -> str:
        resp = self._client.post(self._url, json={
            "model": self.model, "stream": False, "format": INVOICE_SCHEMA,
            # Ollama silently cuts prompts that do not fit num_ctx, and its default is far too small for a document
            "options": {"temperature": 0, "num_ctx": self.num_ctx},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        resp.raise_for_status()
        body = resp.json()
        if body.get("done_reason") == "length":
            raise LLMResponseError("done_reason=length")
        return body["message"]["content"]


def _parse_json(raw: str) -> dict | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(raw[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _text_amounts(text: str) -> set[Decimal]:
    out: set[Decimal] = set()
    for m in NUM_RE.finditer(text):
        v = parse_amount(m.group())
        if v is not None:
            out.add(money(abs(v)))
    return out


def ground(obj: dict, text: str, dayfirst: bool = True) -> tuple[ExtractionResult, list[str]]:
    """Convert an LLM JSON object to an ExtractionResult, dropping anything not present in `text`."""
    res = ExtractionResult()
    dropped: list[str] = []
    norm = norm_text(text)
    amounts = _text_amounts(text)
    dates = {d for d, _a, _r in find_dates(text, dayfirst)} | {d for d, _a, _r in find_dates(text, not dayfirst)}
    evidence = obj.get("evidence") if isinstance(obj.get("evidence"), dict) else {}
    data: dict = {}

    def ev(field: str) -> str | None:
        v = evidence.get(field)
        return v[:160] if isinstance(v, str) else None

    for field in ("invoice_number", "supplier_name", "customer_name"):
        v = obj.get(field)
        if isinstance(v, str) and v.strip():
            v = v.strip()
            if norm_text(v) and norm_text(v) in norm:
                data[field] = v[:300]
                res.meta[field] = FieldMeta(confidence=0.75, source="llm", evidence=ev(field))
            else:
                dropped.append(field)
    v = obj.get("invoice_date")
    if isinstance(v, str) and v.strip():
        try:
            d = date.fromisoformat(v.strip())
            if d in dates:
                data["invoice_date"] = d
                res.meta["invoice_date"] = FieldMeta(confidence=0.75, source="llm", evidence=ev("invoice_date"))
            else:
                dropped.append("invoice_date")
        except ValueError:
            dropped.append("invoice_date")
    for field in AMOUNT_FIELDS:
        amt = parse_amount(obj.get(field)) if obj.get(field) is not None else None
        if amt is not None:
            if money(abs(amt)) in amounts:
                data[field] = amt
                res.meta[field] = FieldMeta(confidence=0.75, source="llm", evidence=ev(field))
            else:
                dropped.append(field)
    cur = obj.get("currency")
    if isinstance(cur, str) and re.fullmatch(r"[A-Za-z]{3}", cur.strip()):
        cur = cur.strip().upper()
        symbol_ok = {"GBP": "£", "EUR": "€", "USD": "$", "BDT": "৳", "INR": "₹", "JPY": "¥"}.get(cur)
        if re.search(rf"\b{cur}\b", text) or (symbol_ok and symbol_ok in text):
            data["currency"] = cur
            res.meta["currency"] = FieldMeta(confidence=0.7, source="llm", evidence=ev("currency"))
        else:
            dropped.append("currency")
    items: list[LineItem] = []
    raw_items = obj.get("line_items")
    for it in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(it, dict):
            continue
        desc = it.get("description") if isinstance(it.get("description"), str) else None
        qty, unit, amt = (parse_amount(it.get(k)) if it.get(k) is not None else None for k in ("quantity", "unit_price", "amount"))
        ok = (desc is None or norm_text(desc) in norm) and all(
            v is None or money(abs(v)) in amounts or k == "quantity" for k, v in (("q", qty), ("unit_price", unit), ("amount", amt))
        )
        if ok and (desc or amt is not None):
            items.append(LineItem(description=desc[:300] if desc else None, quantity=qty, unit_price=unit, amount=amt))
        else:
            dropped.append("line_item")
    if items:
        data["line_items"] = items
        res.meta["line_items"] = FieldMeta(confidence=0.7, source="llm", evidence=f"{len(items)} row(s)")
    try:
        res.data = InvoiceData(**data)
    except Exception as exc:  # defensive: schema bounds
        log.warning("LLM output rejected by schema: %s", exc)
        return ExtractionResult(), ["schema"]
    return res, dropped


def llm_extract(client: LLMClient, text: str, *, max_chars: int, dayfirst: bool = True) -> tuple[ExtractionResult | None, list[str]]:
    """Returns (grounded result or None on failure, list of dropped/ungrounded field names)."""
    prompt = USER_TEMPLATE.format(text=text[:max_chars])
    try:
        raw = client.complete(SYSTEM_PROMPT, prompt)
    except Exception as exc:  # Ollama not running / model missing / 5xx: fall back to rules, never fail the document
        log.warning("LLM call failed: %s", exc)
        return None, ["llm_unavailable"]
    obj = _parse_json(raw)
    if obj is None:
        return None, ["llm_bad_json"]
    return ground(obj, text, dayfirst)
