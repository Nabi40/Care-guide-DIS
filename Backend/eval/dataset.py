"""Build the labeled evaluation dataset (deterministic for a given seed).

Every file is generated from InvoiceSpec objects, so ground truth is exact. The labels describe what
is *printed*, including deliberately injected errors, so extraction is judged on reading faithfully.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import pikepdf

from . import synth

INJECT_CYCLE = [None, None, None, "total_mismatch", None, "line_math", None, None, "missing_number", None]


def _inj(rng: random.Random, n: int) -> str | None:
    return INJECT_CYCLE[n % len(INJECT_CYCLE)]


def build(out: Path, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    out.mkdir(parents=True, exist_ok=True)
    scratch = out / "_src"
    scratch.mkdir(exist_ok=True)
    entries: list[dict] = []
    counter = {"n": 0}
    first_seen: dict[tuple, str] = {}

    def add(path: Path, category: str, specs: list[synth.InvoiceSpec], expect_fail: str | None = None, quality: str | None = None):
        entries.append({
            "file": path.name, "category": category, "quality": quality, "expect_fail": expect_fail,
            "invoices": [s.truth() for s in specs] if specs else [],
        })

    def spec(**kw) -> synth.InvoiceSpec:
        counter["n"] += 1
        return synth.make_spec(rng, counter["n"], **kw)

    # 1. digital PDFs (30 single, one of them multi-page) ------------------------------------------
    for i in range(30):
        s = spec(template=1 + i % 3, inject=_inj(rng, i), n_items=40 if i == 7 else None)
        p = out / f"digital_{i:02d}.pdf"
        synth.render_pdf([s], p)
        add(p, "digital_pdf", [s])

    # 2. multi-invoice digital PDFs (4 files x 6 invoices) ------------------------------------------
    for i in range(4):
        specs = [spec(template=1 + (i + j) % 3, inject=_inj(rng, i * 6 + j + 3)) for j in range(6)]
        p = out / f"batch_{i}.pdf"
        synth.render_pdf(specs, p)
        add(p, "digital_pdf_multi", specs)

    # 3. scanned PDFs: clean / moderate / poor -----------------------------------------------------
    for q_i, quality in enumerate(("clean", "moderate", "poor")):
        for i in range(6):
            s = spec(template=1 + (i + q_i) % 3, inject=_inj(rng, i + q_i), money_style=rng.choice(["symbol", "code"]))
            src = scratch / f"src_{quality}_{i}.pdf"
            synth.render_pdf([s], src)
            p = out / f"scan_{quality}_{i}.pdf"
            synth.save_scan(src, p, quality, rng)
            add(p, f"scan_pdf_{quality}", [s], quality=quality)

    # 4. images: PNG / JPG / TIFF (one 2-frame TIFF) -------------------------------------------------
    for i, (ext, quality) in enumerate([(".png", "moderate")] * 6 + [(".jpg", "moderate")] * 4 + [(".tiff", "clean")] * 3):
        s = spec(template=1 + i % 3, inject=_inj(rng, i + 5), n_items=40 if (ext == ".tiff" and i == 12) else None)
        src = scratch / f"src_img_{i}.pdf"
        synth.render_pdf([s], src)
        p = out / f"image_{i:02d}{ext}"
        synth.save_scan(src, p, quality, rng)
        add(p, f"image_{ext[1:].replace('jpg', 'jpeg')}", [s], quality=quality)

    # 5. spreadsheets -------------------------------------------------------------------------------
    for i in range(6):
        s = spec(inject=_inj(rng, i + 1))
        p = out / f"sheet_{i}.xlsx"
        synth.write_xlsx_single(s, p)
        add(p, "xlsx_single", [s])
    for i in range(4):
        specs = [spec(inject=_inj(rng, i * 5 + j + 2)) for j in range(rng.randint(3, 6))]
        p = out / f"register_{i}.xlsx"
        synth.write_xlsx_register(specs, p, variant=i % 2)
        add(p, "xlsx_register", specs)
    for i in range(3):
        s = spec(inject=_inj(rng, i + 4))
        p = out / f"legacy_{i}.xls"
        synth.write_xls_single(s, p)
        add(p, "xls_single", [s])
    delims = [(",", "utf-8"), (";", "utf-8"), ("\t", "utf-8"), ("|", "cp1252"), (",", "utf-8"), (";", "cp1252")]
    for i, (d, enc) in enumerate(delims):
        specs = [spec(currency="GBP", inject=_inj(rng, i * 4 + j + 1)) for j in range(rng.randint(3, 5))]
        p = out / f"export_{i}.csv"
        synth.write_csv_register(specs, p, delimiter=d, encoding=enc, variant=i % 2, money_symbols=(i % 2 == 1))
        add(p, "csv_register", specs)

    # 6. cross-format duplicates: same invoice arrives again as another file ----------------------
    originals = [e for e in entries if e["category"] in ("digital_pdf",) and not e["invoices"][0]["expected_codes"]][:4]
    for i, e in enumerate(originals):
        t = e["invoices"][0]
        s = spec(template=2)
        s.invoice_number, s.supplier = t["invoice_number"], t["supplier_name"]
        p = out / f"dup_{i}.xlsx"
        synth.write_xlsx_single(s, p)
        entry_specs = [s]
        s.injected = ["DUPLICATE_INVOICE"]
        add(p, "duplicate_xlsx", entry_specs)

    # 7. broken inputs: must fail cleanly with a typed error ---------------------------------------
    bad = {
        "corrupt_garbage.pdf": (b"%PDF-1.4\n" + b"\x00junk" * 200, "corrupted_file"),
        "corrupt_empty_body.pdf": (b"%PDF-1.7\n%%EOF", "corrupted_file"),
        "corrupt_zip.xlsx": (b"PK\x03\x04" + b"not a workbook" * 40, "corrupted_file"),
        "corrupt_ole.xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 300, "corrupted_file"),
        "corrupt_png.png": (b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, "corrupted_file"),
        "corrupt_tiff.tiff": (b"II*\x00" + b"\xff" * 100, "corrupted_file"),
        "corrupt_blank.csv": (b"\n\n  \n", "corrupted_file"),
    }
    for name, (blob, code) in bad.items():
        (out / name).write_bytes(blob)
        add(out / name, "corrupt", [], expect_fail=code)
    # truncated real PDF + truncated real PNG
    src_pdf = next(out / e["file"] for e in entries if e["category"] == "digital_pdf")
    (out / "truncated.pdf").write_bytes(src_pdf.read_bytes()[:200])
    add(out / "truncated.pdf", "corrupt", [], expect_fail="corrupted_file")
    png = next(out / e["file"] for e in entries if e["category"] == "image_png")
    (out / "truncated.png").write_bytes(png.read_bytes()[:400])
    add(out / "truncated.png", "corrupt", [], expect_fail="corrupted_file")
    enc_src = out / "encrypted.pdf"
    with pikepdf.open(src_pdf) as d:
        d.save(enc_src, encryption=pikepdf.Encryption(owner="o", user="u"))
    add(enc_src, "corrupt", [], expect_fail="encrypted_file")
    # a blank page: valid image, no text -> must fail with no_text_extracted (not crash, not invent a record)
    from PIL import Image
    Image.new("L", (1200, 1600), 255).save(out / "blank_scan.png")
    add(out / "blank_scan.png", "corrupt", [], expect_fail="no_text_extracted")

    (out / "manifest.json").write_text(json.dumps(entries, indent=1))
    return entries


def build_stress_pdf(path: Path, pages: int, seed: int = 99) -> list[dict]:
    """A `pages`-page digital PDF holding one invoice per page, with unique numbers."""
    rng = random.Random(seed)
    specs = []
    for n in range(pages):
        s = synth.make_spec(rng, n, template=1 + n % 3, n_items=rng.randint(3, 9))
        s.invoice_number = f"STR-{n + 1:06d}"
        s.supplier = synth.SUPPLIERS[n % len(synth.SUPPLIERS)]
        specs.append(s)
    synth.render_pdf(specs, path)
    return [s.truth() for s in specs]
