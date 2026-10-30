"""Pydantic models shared by extraction, validation and the API."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_STR = 300
MAX_AMOUNT = Decimal("1e12")
_CENT = Decimal("0.01")


def _norm_money(v: Optional[Decimal], *, bound: bool = True) -> Optional[Decimal]:
    """Range-check, and print two-decimal values consistently (4774.1 -> 4774.10). Finer values are kept."""
    if v is None:
        return None
    if not v.is_finite() or abs(v) > MAX_AMOUNT:
        raise ValueError("amount out of range")
    q = v.quantize(_CENT)
    return q if q == v else v


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Optional[str] = Field(default=None, max_length=1000)
    quantity: Optional[Decimal] = None
    unit_price: Optional[Decimal] = None
    amount: Optional[Decimal] = None

    @field_validator("unit_price", "amount")
    @classmethod
    def _money(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _norm_money(v)

    @field_validator("quantity")
    @classmethod
    def _qty(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        if v is not None and (not v.is_finite() or abs(v) > MAX_AMOUNT):
            raise ValueError("quantity out of range")
        return Decimal(int(v)) if v is not None and v == v.to_integral() else v  # 20, not 2E+1


class InvoiceData(BaseModel):
    """Structured result. Every field is nullable: unknown stays null, never guessed."""
    model_config = ConfigDict(extra="forbid")
    invoice_number: Optional[str] = Field(default=None, max_length=MAX_STR)
    invoice_date: Optional[date] = None
    supplier_name: Optional[str] = Field(default=None, max_length=MAX_STR)
    customer_name: Optional[str] = Field(default=None, max_length=MAX_STR)
    subtotal: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    total: Optional[Decimal] = None
    currency: Optional[str] = Field(default=None, max_length=3, min_length=3)
    line_items: list[LineItem] = Field(default_factory=list, max_length=5000)

    @field_validator("subtotal", "tax", "total")
    @classmethod
    def _bounded(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _norm_money(v)

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: Optional[str]) -> Optional[str]:
        return v.upper() if v else v


SCALAR_FIELDS = [
    "invoice_number", "invoice_date", "supplier_name", "customer_name",
    "subtotal", "tax", "total", "currency",
]
REQUIRED_FIELDS = ["invoice_number", "invoice_date", "supplier_name", "total"]


class FieldMeta(BaseModel):
    confidence: float = 0.0
    source: str = "rules"  # rules | sheet | llm | manual
    evidence: Optional[str] = None


class ExtractionResult(BaseModel):
    data: InvoiceData = Field(default_factory=InvoiceData)
    meta: dict[str, FieldMeta] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    ambiguous_date: bool = False
    # unreliable-source flags raised by the extractor (e.g. LLM disagreed with rules)
    flags: list[str] = Field(default_factory=list)

    def confidence(self) -> float:
        vals = [self.meta[f].confidence if f in self.meta and getattr(self.data, f) is not None else 0.0
                for f in REQUIRED_FIELDS]
        return round(sum(vals) / len(vals), 3)


class InvoicePatch(BaseModel):
    """Manual correction. Only fields present in the request body are applied (null clears)."""
    model_config = ConfigDict(extra="forbid")
    invoice_number: Optional[str] = Field(default=None, max_length=MAX_STR)
    invoice_date: Optional[date] = None
    supplier_name: Optional[str] = Field(default=None, max_length=MAX_STR)
    customer_name: Optional[str] = Field(default=None, max_length=MAX_STR)
    subtotal: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    total: Optional[Decimal] = None
    currency: Optional[str] = Field(default=None, max_length=3, min_length=3)
    line_items: Optional[list[LineItem]] = Field(default=None, max_length=5000)

    @field_validator("subtotal", "tax", "total")
    @classmethod
    def _bounded(cls, v: Optional[Decimal]) -> Optional[Decimal]:
        return _norm_money(v)


class ApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: Optional[str] = Field(default=None, max_length=1000)
