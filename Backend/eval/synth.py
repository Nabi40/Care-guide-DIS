"""Synthetic invoice generator with exact ground truth.

Used by the test-suite and the evaluation. Documents are *printed* from a spec, so the label is
what the document literally says (including deliberately injected arithmetic errors).
"""
from __future__ import annotations

import csv
import io
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

CENT = Decimal("0.01")
SUPPLIERS = ["Northwind Traders Ltd", "Blue Harbour Supplies", "Kestrel Engineering Ltd", "Orchid Digital Services",
             "Summit Logistics Plc", "Greenfield Foods Ltd", "Atlas Office Solutions", "Pioneer Print & Packaging",
             "Cedar Valley Consulting", "Redwood Hardware Ltd", "Silverline Software", "Harbor Light Catering",
             "Meridian Cleaning Services", "Falcon Security Ltd", "Brightwave Telecom"]
CUSTOMERS = ["Dunmore Holdings Ltd", "Eastgate Manufacturing", "Lakeside Clinic Group", "Oakwood Retail Ltd",
             "Primrose Property Management", "Tamar Valley Farms", "Union Street Studios", "Vertex Analytics Ltd",
             "Westbridge Education Trust", "Yorkshire Dales Hotels"]
ITEMS = ["Consulting services", "Software licence (annual)", "Office chairs", "A4 paper (box of 5)", "Laptop stand",
         "Cloud hosting - monthly", "Security audit", "Printer toner cartridge", "Courier delivery", "Training workshop",
         "Network switch 24-port", "Maintenance contract", "Catering - lunch buffet", "Cleaning service", "Design work (hours)",
         "Steel brackets (pack of 50)", "LED panel light", "Data backup service", "Website development", "Travel expenses"]
CURRENCY_SYMBOL = {"GBP": "£", "USD": "$", "EUR": "€"}


def q2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Item:
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal  # as printed (may be tampered)


@dataclass
class InvoiceSpec:
    invoice_number: str | None  # None => number is not printed anywhere
    invoice_date: date
    supplier: str
    customer: str
    currency: str
    items: list[Item]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    tax_rate: Decimal
    template: int = 1
    date_style: str = "dmy_long"
    money_style: str = "symbol"  # symbol | code | plain | eu
    injected: list[str] = field(default_factory=list)  # expected validation codes

    def truth(self) -> dict:
        return {
            "invoice_number": self.invoice_number,
            "invoice_date": self.invoice_date.isoformat(),
            "supplier_name": self.supplier,
            "customer_name": self.customer,
            "subtotal": str(self.subtotal), "tax": str(self.tax), "total": str(self.total),
            "currency": self.currency,
            "line_items": [{"description": i.description, "quantity": str(i.quantity),
                            "unit_price": str(i.unit_price), "amount": str(i.amount)} for i in self.items],
            "expected_codes": list(self.injected),
        }


def make_spec(rng: random.Random, n: int, *, template: int | None = None, n_items: int | None = None,
              currency: str | None = None, inject: str | None = None, date_style: str | None = None,
              money_style: str | None = None) -> InvoiceSpec:
    template = template or rng.choice([1, 2, 3])
    currency = currency or rng.choice(["GBP", "GBP", "USD", "EUR"])
    k = n_items or rng.randint(2, 7)
    items = []
    descs = rng.sample(ITEMS, min(k, len(ITEMS)))
    descs += [f"{rng.choice(ITEMS)} (batch {n})" for n in range(max(0, k - len(ITEMS)))]
    for desc in descs:
        qty = Decimal(rng.choice([1, 1, 2, 3, 5, 10, 12, 20])) if "hours" not in desc else Decimal(rng.choice(["2.5", "7.5", "4"]))
        unit = Decimal(rng.randint(500, 45000)) / 100
        items.append(Item(desc, qty, q2(unit), q2(qty * unit)))
    subtotal = q2(sum((i.amount for i in items), Decimal(0)))
    rate = Decimal(rng.choice(["0.20", "0.20", "0.05", "0.10", "0.15"]))
    tax = q2(subtotal * rate)
    total = subtotal + tax
    injected: list[str] = []
    number: str | None = f"{rng.choice(['INV', 'SI', 'BL', 'AC'])}-{2026}-{rng.randint(1000, 99999):05d}"
    if inject == "total_mismatch":
        total = total + Decimal(rng.choice(["10.00", "25.50", "100.00", "-7.25"]))
        injected = ["TOTAL_MISMATCH"]
    elif inject == "line_math":
        j = rng.randrange(len(items))
        items[j].amount = items[j].amount + Decimal(rng.choice(["5.00", "12.34", "-3.50"]))
        injected = ["LINE_ITEM_MATH", "SUBTOTAL_MISMATCH"]
    elif inject == "missing_number":
        number = None
        injected = ["MISSING_FIELD"]
    d = date(2026, 1, 1) + timedelta(days=rng.randint(0, 270))
    if date_style is None:
        date_style = rng.choice(["dmy_long", "dmy_slash", "iso", "mdy_long"])
    if date_style == "dmy_slash" and d.day > 12 and rng.random() < 0.5:
        d = d.replace(day=rng.randint(1, 12))  # make some slash dates genuinely ambiguous
    return InvoiceSpec(number, d, rng.choice(SUPPLIERS), rng.choice(CUSTOMERS), currency, items, subtotal, tax, total,
                       rate, template, date_style, money_style or rng.choice(["symbol", "code", "plain"]), injected)


# --------------------------------------------------------------------------- formatting
def fmt_date(d: date, style: str) -> str:
    if style == "dmy_long":
        return d.strftime("%d %b %Y")
    if style == "dmy_slash":
        return d.strftime("%d/%m/%Y")
    if style == "iso":
        return d.isoformat()
    return d.strftime("%B %d, %Y").replace(" 0", " ")


def fmt_money(v: Decimal, cur: str, style: str, eu: bool = False) -> str:
    s = f"{v:,.2f}"
    if eu:
        s = s.replace(",", "X").replace(".", ",").replace("X", ".")
        return f"{s} €" if cur == "EUR" else s
    if style == "symbol":
        return f"{CURRENCY_SYMBOL.get(cur, '')}{s}"
    if style == "code":
        return f"{cur} {s}"
    return s


def fmt_qty(q: Decimal) -> str:
    return str(int(q)) if q == q.to_integral() else str(q)


# --------------------------------------------------------------------------- PDF rendering
W, H = A4


def _txt(c, x, y, s, size=10, bold=False, right=False, center=False):
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    (c.drawRightString if right else c.drawCentredString if center else c.drawString)(x, y, s)


def _header(c, spec: InvoiceSpec, cont: bool) -> float:
    t = spec.template
    d = fmt_date(spec.invoice_date, spec.date_style)
    num = spec.invoice_number
    if t == 1:
        _txt(c, 40, 800, spec.supplier, 16, True)
        if not cont:
            _txt(c, 40, 786, "12 Mill Road, Carlisle CA1 1AA", 9)
            _txt(c, 40, 774, "VAT No: GB123456789", 9)
        _txt(c, 555, 800, "INVOICE", 20, True, right=True)
        if num:
            _txt(c, 555, 778, f"Invoice No: {num}", 10, right=True)
        if not cont:
            _txt(c, 555, 764, f"Invoice Date: {d}", 10, right=True)
            _txt(c, 555, 750, f"Due Date: {fmt_date(spec.invoice_date + timedelta(days=30), spec.date_style)}", 10, right=True)
            _txt(c, 40, 720, "Bill To:", 10, True)
            _txt(c, 40, 706, spec.customer, 10)
            _txt(c, 40, 694, "88 Station Approach, Leeds LS1 4AB", 9)
            return 650
        return 740
    if t == 2:
        _txt(c, W / 2, 805, "TAX INVOICE", 18, True, center=True)
        if not cont:
            _txt(c, 40, 770, "From:", 10, True)
            _txt(c, 40, 756, spec.supplier, 11)
            _txt(c, 40, 744, "Unit 4, Quay Business Park", 9)
            _txt(c, 320, 770, "Bill To:", 10, True)
            _txt(c, 320, 756, spec.customer, 11)
            _txt(c, 320, 744, "PO Box 55, Manchester M1 2AB", 9)
            if num:
                _txt(c, 40, 700, f"Invoice #: {num}", 10)
            _txt(c, 320, 700, f"Date: {d}", 10)
            return 660
        if num:
            _txt(c, 40, 770, f"{spec.supplier}   Invoice #: {num}", 10)
        return 740
    # template 3
    _txt(c, 40, 805, "INVOICE", 18, True)
    if not cont:
        _txt(c, 40, 775, "Seller:", 10, True)
        _txt(c, 110, 775, spec.supplier, 10)
        _txt(c, 40, 760, "Customer:", 10, True)
        _txt(c, 110, 760, spec.customer, 10)
        if num:
            _txt(c, 40, 735, "Inv. No.", 10, True)
            _txt(c, 110, 735, num, 10)
        _txt(c, 40, 720, "Date of issue:", 10, True)
        _txt(c, 125, 720, d, 10)
        _txt(c, 40, 705, "Currency:", 10, True)
        _txt(c, 110, 705, spec.currency, 10)
        return 670
    if num:
        _txt(c, 40, 775, f"Inv. No. {num}", 10)
    return 740


_COLS = {
    1: ["Description", "Qty", "Unit Price", "Amount"],
    2: ["Item", "Quantity", "Price", "Total"],
    3: ["Description", "Qty", "Rate", "Line Total"],
}
_TOT = {1: ("Subtotal", "VAT ({r}%)", "Total"), 2: ("Subtotal", "Tax ({r}%)", "Total Due"),
        3: ("Net Total", "Sales Tax", "Grand Total")}


def render_pdf(specs: list[InvoiceSpec], path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    for spec in specs:
        eu = spec.template == 3 and spec.currency == "EUR"
        t = spec.template
        y = _header(c, spec, cont=False)
        cols = _COLS[t]

        def table_head(y):
            _txt(c, 40, y, cols[0], 10, True)
            _txt(c, 380, y, cols[1], 10, True, right=True)
            _txt(c, 470, y, cols[2], 10, True, right=True)
            _txt(c, 555, y, cols[3], 10, True, right=True)
            c.line(40, y - 4, 555, y - 4)
            return y - 20

        y = table_head(y)
        for it in spec.items:
            if y < 150:
                c.showPage()
                y = table_head(_header(c, spec, cont=True))
            _txt(c, 40, y, it.description, 10)
            _txt(c, 380, y, fmt_qty(it.quantity), 10, right=True)
            _txt(c, 470, y, fmt_money(it.unit_price, spec.currency, spec.money_style, eu), 10, right=True)
            _txt(c, 555, y, fmt_money(it.amount, spec.currency, spec.money_style, eu), 10, right=True)
            y -= 16
        if y < 120:
            c.showPage()
            y = _header(c, spec, cont=True)
        y -= 14
        sub_l, tax_l, tot_l = _TOT[t]
        rate = int(spec.tax_rate * 100)
        for label, val, bold in ((sub_l, spec.subtotal, False), (tax_l.format(r=rate), spec.tax, False), (tot_l, spec.total, True)):
            _txt(c, 400, y, label, 10, bold)
            _txt(c, 555, y, fmt_money(val, spec.currency, spec.money_style, eu), 10, bold, right=True)
            y -= 16
        _txt(c, 40, 60, "Payment terms: 30 days. Thank you for your business.", 8)
        if spec.money_style == "plain" and t != 3:
            _txt(c, 40, 48, f"All amounts in {spec.currency}", 8)
        c.showPage()
    c.save()


# --------------------------------------------------------------------------- scans
def rasterize(pdf_path: Path, dpi: int = 150) -> list[Image.Image]:
    out = []
    doc = pdfium.PdfDocument(pdf_path)
    try:
        for page in doc:
            out.append(page.render(scale=dpi / 72, grayscale=True).to_pil().convert("L"))
            page.close()
    finally:
        doc.close()
    return out


def degrade(img: Image.Image, level: str, rng: random.Random) -> Image.Image:
    if level == "clean":
        return img
    p = {"moderate": (1.0, 8, 0.6, 60, 1.0), "poor": (2.5, 18, 1.0, 35, 0.72)}[level]
    angle, sigma, blur, jq, scale = p
    img = img.rotate(rng.uniform(-angle, angle), expand=False, fillcolor=255, resample=Image.BICUBIC)
    if scale < 1:
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.BILINEAR)
    img = img.filter(ImageFilter.GaussianBlur(blur))
    arr = np.asarray(img, dtype=np.float32)
    nrng = np.random.default_rng(rng.randint(0, 2**31))
    arr = arr + nrng.normal(0, sigma, arr.shape)
    if level == "poor":
        arr = 255 - (255 - arr) * 0.8  # washed-out toner
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=jq)
    return Image.open(io.BytesIO(buf.getvalue())).convert("L")


def save_scan(pdf_path: Path, out: Path, level: str, rng: random.Random) -> None:
    pages = [degrade(p, level, rng) for p in rasterize(pdf_path)]
    ext = out.suffix.lower()
    if ext == ".pdf":
        pages[0].convert("RGB").save(out, "PDF", resolution=150, save_all=True,
                                     append_images=[p.convert("RGB") for p in pages[1:]])
    elif ext in (".tif", ".tiff"):
        pages[0].save(out, "TIFF", save_all=True, append_images=pages[1:], compression="tiff_lzw")
    elif ext in (".jpg", ".jpeg"):
        pages[0].convert("RGB").save(out, "JPEG", quality=85)
    else:
        pages[0].save(out, "PNG")


# --------------------------------------------------------------------------- spreadsheets
REGISTER_HEADERS = [
    ["Invoice Number", "Invoice Date", "Supplier", "Customer", "Currency", "Description", "Qty", "Unit Price",
     "Amount", "Subtotal", "Tax", "Total"],
    ["Inv No", "Date", "Vendor", "Client", "Currency", "Item", "Quantity", "Price", "Line Total", "Net Total", "VAT", "Grand Total"],
]


def register_rows(specs: list[InvoiceSpec], variant: int, as_text: bool = False, date_fmt: str = "iso") -> list[list]:
    rows: list[list] = [REGISTER_HEADERS[variant]]
    for s in specs:
        for it in s.items:
            d = s.invoice_date
            dv = d.isoformat() if date_fmt == "iso" else d.strftime("%d/%m/%Y")
            if not as_text and date_fmt == "iso":
                dv = datetime(d.year, d.month, d.day)
            num = (lambda v: str(v)) if as_text else (lambda v: float(v))
            rows.append([s.invoice_number, dv, s.supplier, s.customer, s.currency, it.description,
                         num(it.quantity), num(it.unit_price), num(it.amount),
                         num(s.subtotal), num(s.tax), num(s.total)])
    return rows


def write_xlsx_register(specs: list[InvoiceSpec], path: Path, variant: int = 0) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Register"
    for r in register_rows(specs, variant):
        ws.append(r)
    notes = wb.create_sheet("Notes")
    notes.append(["Exported from ERP"])
    notes.append(["Prepared by finance team"])
    wb.save(path)


def write_xlsx_single(spec: InvoiceSpec, path: Path) -> None:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice"
    ws.append(["Supplier", spec.supplier])
    ws.append(["Invoice No", spec.invoice_number] if spec.invoice_number else ["Reference", None])
    ws.append(["Invoice Date", datetime(spec.invoice_date.year, spec.invoice_date.month, spec.invoice_date.day)])
    ws.append(["Customer", spec.customer])
    ws.append(["Currency", spec.currency])
    ws.append([])
    ws.append(["Description", "Qty", "Unit Price", "Amount"])
    for it in spec.items:
        ws.append([it.description, float(it.quantity), float(it.unit_price), float(it.amount)])
    ws.append([])
    ws.append([None, None, "Subtotal", float(spec.subtotal)])
    ws.append([None, None, "Tax", float(spec.tax)])
    ws.append([None, None, "Total", float(spec.total)])
    n = wb.create_sheet("Notes")
    n.append(["Payment by bank transfer within 30 days."])
    wb.save(path)


def write_xls_single(spec: InvoiceSpec, path: Path) -> None:
    import xlwt

    wb = xlwt.Workbook()
    ws = wb.add_sheet("Invoice")
    rows = [["Supplier", spec.supplier], ["Invoice No", spec.invoice_number or ""],
            ["Invoice Date", spec.invoice_date.isoformat()], ["Customer", spec.customer], ["Currency", spec.currency], [],
            ["Description", "Qty", "Unit Price", "Amount"]]
    rows += [[i.description, float(i.quantity), float(i.unit_price), float(i.amount)] for i in spec.items]
    rows += [[], ["", "", "Subtotal", float(spec.subtotal)], ["", "", "Tax", float(spec.tax)], ["", "", "Total", float(spec.total)]]
    for r, row in enumerate(rows):
        for c_, v in enumerate(row):
            ws.write(r, c_, v)
    wb.save(str(path))


def write_csv_register(specs: list[InvoiceSpec], path: Path, delimiter: str = ",", encoding: str = "utf-8",
                       variant: int = 0, money_symbols: bool = False) -> None:
    rows = register_rows(specs, variant, as_text=True, date_fmt="dmy")
    if money_symbols:
        sym = lambda s, v: f"{CURRENCY_SYMBOL.get(s.currency, '')}{Decimal(v):,.2f}"
        flat = [s for s in specs for _ in s.items]
        for r, s in zip(rows[1:], flat):
            for j in (7, 8, 9, 10, 11):
                r[j] = sym(s, r[j])
    buf = io.StringIO()
    csv.writer(buf, delimiter=delimiter, lineterminator="\r\n").writerows(rows)
    path.write_bytes(buf.getvalue().encode(encoding, errors="replace"))
