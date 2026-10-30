"""Document processing: PDF text layer, OCR, images, spreadsheets, and every way a file can be broken."""
from __future__ import annotations

import io
import random
import zipfile
from pathlib import Path

import pikepdf
import pypdfium2 as pdfium
import pytest
from PIL import Image, ImageDraw

from app.config import Settings
from app.loaders import detect_kind, load_document
from app.loaders.base import (
    CorruptedFileError, EncryptedFileError, LimitExceededError, UnsupportedFileError,
)
from app.loaders.layout import Word, words_to_layout
from app.loaders.ocr import ocr_image
from eval import synth


@pytest.fixture()
def S(tmp_path):
    return Settings(data_dir=tmp_path, ocr_workers=2)


def merge_pdfs(sources, target):
    out = pdfium.PdfDocument.new()
    opened = [pdfium.PdfDocument(src) for src in sources]
    for d in opened:
        out.import_pages(d)
    out.save(target)
    out.close()
    for d in opened:
        d.close()


# ------------------------------------------------------------------ PDF
def test_digital_pdf_uses_text_layer_not_ocr(S, make_pdf):
    path, spec = make_pdf()
    doc = load_document(path, "x.pdf", S)
    assert doc.text_method == "digital" and doc.ocr_pages == 0
    assert spec.invoice_number in doc.pages[0].text
    assert "Subtotal" in doc.pages[0].text or "Net Total" in doc.pages[0].text


def test_layout_keeps_columns_apart_and_phrases_together():
    words = [Word(10, 10, 60, 22, "Acme"), Word(64, 10, 110, 22, "Trading"), Word(114, 10, 140, 22, "Ltd"),
             Word(400, 10, 450, 22, "INVOICE"), Word(10, 40, 60, 52, "Qty"), Word(300, 40, 340, 52, "Price")]
    lines = [l for l in words_to_layout(words).splitlines() if l.strip()]  # (a blank line marks a paragraph gap)
    assert lines[0].startswith("Acme Trading Ltd") and "   INVOICE" in lines[0]
    assert "Qty" in lines[1] and "   Price" in lines[1]


def test_scanned_pdf_goes_through_ocr(S, make_pdf, rng, work):
    path, spec = make_pdf(template=1, currency="GBP", money_style="symbol")
    scan = work / "scan.pdf"
    synth.save_scan(path, scan, "clean", rng)
    doc = load_document(scan, "scan.pdf", S)
    assert doc.text_method == "ocr" and doc.ocr_pages == 1
    assert doc.avg_ocr_confidence and doc.avg_ocr_confidence > 70
    assert spec.invoice_number.replace("-", "")[:6] in doc.pages[0].text.replace("-", "")


def test_mixed_pdf_ocrs_only_pages_without_text(S, make_pdf, rng, work):
    digital, _ = make_pdf("d.pdf")
    scan = work / "s.pdf"
    other, _ = make_pdf("o.pdf")
    synth.save_scan(other, scan, "clean", rng)
    merged = work / "mixed.pdf"
    merge_pdfs((digital, scan, digital), merged)
    doc = load_document(merged, "mixed.pdf", S)
    assert [p.method for p in doc.pages] == ["digital", "ocr", "digital"]
    assert doc.text_method == "mixed" and doc.ocr_pages == 1


def test_multipage_invoice_continues_on_next_page(S, rng, work):
    spec = synth.make_spec(rng, 1, template=1, n_items=40)  # more rows than fit on one page
    p = work / "long.pdf"
    synth.render_pdf([spec], p)
    doc = load_document(p, "long.pdf", S)
    assert len(doc.pages) >= 2
    for it in spec.items:
        assert any(it.description in pg.text for pg in doc.pages)


def test_encrypted_pdf(S, make_pdf, work):
    path, _ = make_pdf()
    enc = work / "enc.pdf"
    with pikepdf.open(path) as d:
        d.save(enc, encryption=pikepdf.Encryption(owner="o", user="u"))
    with pytest.raises(EncryptedFileError):
        load_document(enc, "enc.pdf", S)


@pytest.mark.parametrize("content", [b"%PDF-1.4\n" + b"\x00junk" * 100, b"%PDF-1.7\n%%EOF"])
def test_corrupted_pdf(S, work, content):
    p = work / "bad.pdf"
    p.write_bytes(content)
    with pytest.raises(CorruptedFileError):
        load_document(p, "bad.pdf", S)


def test_truncated_pdf_recovers_or_fails_cleanly(S, make_pdf, work):
    path, _ = make_pdf()
    cut = work / "cut.pdf"
    cut.write_bytes(path.read_bytes()[: len(path.read_bytes()) // 2])
    try:
        doc = load_document(cut, "cut.pdf", S)
        assert doc.pages  # PDFium repaired it
    except CorruptedFileError:
        pass  # also acceptable: a clear, typed error


def test_pdf_page_limit_enforced(tmp_path, make_pdf, work):
    path, _ = make_pdf()
    merge_pdfs([path] * 5, work / "five.pdf")
    with pytest.raises(LimitExceededError):
        load_document(work / "five.pdf", "five.pdf", Settings(data_dir=tmp_path, max_pdf_pages=3))


def test_ocr_disabled_warns_instead_of_crashing(tmp_path, make_pdf, rng, work):
    path, _ = make_pdf()
    scan = work / "s.pdf"
    synth.save_scan(path, scan, "clean", rng)
    doc = load_document(scan, "s.pdf", Settings(data_dir=tmp_path, ocr_enabled=False))
    assert any("OCR is disabled" in w for w in doc.warnings)


# ------------------------------------------------------------------ images
def _text_image(text="INVOICE No: INV-2026-00042   Total GBP 1,234.50", size=(1400, 300)) -> Image.Image:
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    from PIL import ImageFont
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 42)
    except OSError:
        font = ImageFont.load_default()
    d.text((40, 100), text, fill=0, font=font)
    return img


@pytest.mark.parametrize("ext,fmt", [(".png", "PNG"), (".jpg", "JPEG"), (".tiff", "TIFF")])
def test_image_formats_are_ocred(S, work, ext, fmt):
    p = work / f"img{ext}"
    _text_image().convert("RGB" if fmt == "JPEG" else "L").save(p, fmt)
    doc = load_document(p, p.name, S)
    assert "INV-2026-00042" in doc.pages[0].text and doc.text_method == "ocr"


def test_multiframe_tiff_one_page_per_frame(S, work):
    p = work / "multi.tif"
    a, b = _text_image("Invoice No: AAA-111"), _text_image("Invoice No: BBB-222")
    a.save(p, "TIFF", save_all=True, append_images=[b])
    doc = load_document(p, "multi.tif", S)
    assert len(doc.pages) == 2
    assert "AAA-111" in doc.pages[0].text and "BBB-222" in doc.pages[1].text


def test_rgba_and_palette_images_are_handled(S, work):
    p = work / "rgba.png"
    _text_image().convert("RGBA").save(p)
    assert "INV-2026-00042" in load_document(p, "rgba.png", S).pages[0].text


def test_low_quality_image_gets_warning_not_crash(S, work):
    p = work / "noise.png"
    rnd = random.Random(1)
    img = Image.new("L", (600, 400))
    img.putdata([rnd.randint(0, 255) for _ in range(600 * 400)])
    img.save(p)
    doc = load_document(p, "noise.png", S)
    assert doc.pages and (not doc.pages[0].text.strip() or any("Low OCR confidence" in w for w in doc.warnings))


def test_rotated_scan_is_recovered_via_orientation_detection(S):
    img = _text_image().rotate(180, expand=True, fillcolor=255)
    res = ocr_image(img)
    assert "INV-2026-00042" in res.text


@pytest.mark.parametrize("name,content", [
    ("t.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64),
    ("t.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 64),
    ("t.tif", b"II*\x00" + b"\xff" * 64),
])
def test_corrupted_images(S, work, name, content):
    p = work / name
    p.write_bytes(content)
    with pytest.raises(CorruptedFileError):
        load_document(p, name, S)


def test_truncated_png(S, work):
    buf = io.BytesIO()
    _text_image().save(buf, "PNG")
    p = work / "cut.png"
    p.write_bytes(buf.getvalue()[:300])
    with pytest.raises(CorruptedFileError):
        load_document(p, "cut.png", S)


def test_decompression_bomb_rejected(tmp_path, work):
    p = work / "huge.png"
    Image.new("L", (4000, 4000), 255).save(p)
    with pytest.raises(LimitExceededError):
        load_document(p, "huge.png", Settings(data_dir=tmp_path, max_image_pixels=1_000_000))


# ------------------------------------------------------------------ spreadsheets
def test_xlsx_multiple_sheets(S, rng, work):
    specs = [synth.make_spec(rng, n) for n in range(3)]
    p = work / "reg.xlsx"
    synth.write_xlsx_register(specs, p)
    doc = load_document(p, "reg.xlsx", S)
    assert [s.name for s in doc.sheets] == ["Register", "Notes"]
    assert doc.text_method == "sheet" and len(doc.pages) == 2
    assert specs[0].invoice_number in doc.pages[0].text


def test_xls_legacy_format(S, rng, work):
    spec = synth.make_spec(rng, 1)
    p = work / "old.xls"
    synth.write_xls_single(spec, p)
    doc = load_document(p, "old.xls", S)
    assert doc.kind == "xls" and spec.invoice_number in doc.pages[0].text


@pytest.mark.parametrize("delim,enc", [(",", "utf-8"), (";", "utf-8"), ("\t", "utf-8"), ("|", "cp1252")])
def test_csv_dialects_and_encodings(S, rng, work, delim, enc):
    specs = [synth.make_spec(rng, n, currency="GBP") for n in range(2)]
    p = work / "r.csv"
    synth.write_csv_register(specs, p, delimiter=delim, encoding=enc, money_symbols=True)
    doc = load_document(p, "r.csv", S)
    header = doc.sheets[0].rows[0]
    assert header[0] == "Invoice Number" and len(header) == 12  # delimiter detected
    assert any("£" in str(c) for r in doc.sheets[0].rows for c in r)  # encoding decoded


def test_csv_utf16(S, work):
    p = work / "u16.csv"
    p.write_bytes("Invoice Number,Total\nINV-1,10.00\n".encode("utf-16"))
    assert load_document(p, "u16.csv", S).sheets[0].rows[1][0] == "INV-1"


def test_empty_and_blank_spreadsheets(S, work):
    import openpyxl
    p = work / "blank.xlsx"
    openpyxl.Workbook().save(p)
    with pytest.raises(CorruptedFileError):
        load_document(p, "blank.xlsx", S)
    c = work / "blank.csv"
    c.write_text("\n\n")
    with pytest.raises(CorruptedFileError):
        load_document(c, "blank.csv", S)


def test_corrupted_xlsx_and_xls(S, work):
    x = work / "bad.xlsx"
    x.write_bytes(b"PK\x03\x04" + b"not really a zip" * 20)
    with pytest.raises(CorruptedFileError):
        load_document(x, "bad.xlsx", S)
    # valid zip, but not a workbook
    z = work / "zip.xlsx"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("hello.txt", "hi")
    with pytest.raises(CorruptedFileError):
        load_document(z, "zip.xlsx", S)
    o = work / "bad.xls"
    o.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 100)
    with pytest.raises(CorruptedFileError):
        load_document(o, "bad.xls", S)


def test_sheet_row_limit_truncates_with_warning(tmp_path, rng, work):
    specs = [synth.make_spec(rng, n, n_items=5) for n in range(10)]
    p = work / "big.xlsx"
    synth.write_xlsx_register(specs, p)
    doc = load_document(p, "big.xlsx", Settings(data_dir=tmp_path, max_sheet_rows=10))
    assert any("truncated" in w for w in doc.warnings)
    assert len(doc.sheets[0].rows) == 10


def test_xlsx_zip_bomb_guard(tmp_path, work, monkeypatch):
    import app.loaders.spreadsheet as sp
    monkeypatch.setattr(sp, "MAX_UNCOMPRESSED", 10)
    p = work / "ok.xlsx"
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["a", "b"])
    wb.save(p)
    with pytest.raises(LimitExceededError):
        load_document(p, "ok.xlsx", Settings(data_dir=tmp_path))


# ------------------------------------------------------------------ type detection
def test_detect_kind_rules():
    assert detect_kind(b"%PDF-1.7 ...", "a.PDF")[0] == "pdf"
    assert detect_kind(b"\x89PNG\r\n\x1a\n", "a.jpg")[0] == "image"  # content wins over extension within images
    kind, warns = detect_kind(b"PK\x03\x04", "a.xls")
    assert kind == "xlsx" and warns
    for head, name in [(b"MZ\x90", "a.pdf"), (b"%PDF-", "a.png"), (b"hello", "a.docx"), (b"", "a.csv"),
                       (b"a,b\x00\x00", "a.csv"), (b"%PDF-1.4", "a.csv")]:
        with pytest.raises(UnsupportedFileError):
            detect_kind(head, name)
