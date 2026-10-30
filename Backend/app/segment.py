"""Split a multi-page document into invoice segments (a 1000-page PDF holds many invoices)."""
from __future__ import annotations

from dataclasses import dataclass

import re

from .extraction.rules import find_invoice_number
from .loaders.base import PageContent
from .utils import normalize_number


_HEADER_DATE = re.compile(r"(?im)\b(?:invoice\s+date|date\s+of\s+issue|issue\s+date|date)\s*[:\-]?\s*\S")
_HEADER_PARTY = re.compile(r"(?im)\b(?:bill(?:ed)?\s+to|sold\s+to|customer|client)\b")
_HAS_TOTAL = re.compile(r"(?im)^\s*\S.*?\s{2,}(?:grand\s+)?total\b|^\s*(?:grand\s+)?total\b")


def _starts_new_invoice(page_text: str, current_text: str) -> bool:
    """A numberless page that opens with a full invoice header after a *finished* invoice is a new invoice
    that is missing its number, not a continuation page (continuations don't repeat date + bill-to)."""
    return bool(_HEADER_DATE.search(page_text) and _HEADER_PARTY.search(page_text) and _HAS_TOTAL.search(current_text))


@dataclass
class Segment:
    pages: list[int]
    text: str


def segment_pages(pages: list[PageContent]) -> list[Segment]:
    """New segment whenever a page shows an invoice number different from the current one.

    Pages without a number are continuation pages (line-item overflow, terms, remittance slips).
    If no page carries a number the whole document is one segment.
    """
    segs: list[tuple[str | None, list[PageContent]]] = []
    for p in pages:
        found = find_invoice_number(p.text)
        key = normalize_number(found[0]) if found else None
        if key and (not segs or segs[-1][0] != key):
            if len(segs) == 1 and segs[-1][0] is None:
                # leading pages without a number belong to this first numbered invoice
                segs[-1] = (key, segs[-1][1] + [p])
            else:
                segs.append((key, [p]))
        elif segs and not key and _starts_new_invoice(p.text, "\n\n".join(g.text for g in segs[-1][1])):
            segs.append((None, [p]))
        elif segs:
            segs[-1][1].append(p)
        else:
            segs.append((None, [p]))
    out = []
    for _key, group in segs:
        text = "\n\n".join(g.text for g in group)
        if text.strip():
            out.append(Segment([g.page_no for g in group], text))
    return out
