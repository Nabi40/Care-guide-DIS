"""Measure the pipeline on the labeled dataset and write eval/report.md + eval/results.json.

    python -m eval.run_eval [--seed 7] [--stress-digital 1000] [--stress-scan 40] [--llm]

--llm enables AI extraction (needs a running Ollama with LLM_MODEL pulled). Processing runs through the same `run_document` used by the API workers, one document at a time
(so timings are per-document latencies and duplicate-detection order is deterministic).
"""
from __future__ import annotations

import argparse
import difflib
import json
import platform
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Base, Document, Invoice, make_engine, make_session_factory  # noqa: E402
from app.loaders import detect_kind, sniff_head  # noqa: E402
from app.pipeline import run_document  # noqa: E402
from app.search import make_embedder  # noqa: E402
from app.services import Services, make_llm  # noqa: E402
from eval import dataset, synth  # noqa: E402

try:
    import resource
except ImportError:  # Windows
    resource = None

LLM = None  # set by --llm
FIELDS =["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
AMOUNTS = {"subtotal", "tax", "total"}


def make_services(workdir: Path, **over) -> Services:
    S = Settings(data_dir=workdir, retry_backoff_s=0.0, **over)
    S.upload_dir.mkdir(parents=True, exist_ok=True)
    engine = make_engine(S.db_url)
    Base.metadata.create_all(engine)
    return Services(S, make_session_factory(engine), make_embedder("hashing", ""), LLM)


def peak_rss_mb() -> int | None:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024) if resource else None


def ingest(svc: Services, path: Path) -> str:
    import hashlib
    import uuid
    raw = path.read_bytes()
    doc_id = str(uuid.uuid4())
    stored = f"{doc_id}{path.suffix.lower()}"
    (svc.settings.upload_dir / stored).write_bytes(raw)
    try:
        kind, _ = detect_kind(raw[:8192], path.name)
    except Exception:
        kind = "unknown"
    with svc.session_factory() as s:
        s.add(Document(id=doc_id, filename=path.name, stored_name=stored, kind=kind, size_bytes=len(raw),
                       sha256=hashlib.sha256(raw).hexdigest(), status="queued", warnings=[]))
        s.commit()
    return doc_id


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().casefold()


def field_equal(f: str, got, truth) -> bool:
    if truth is None:
        return got is None
    if got is None:
        return False
    if f in AMOUNTS:
        return Decimal(str(got)) == Decimal(str(truth))
    return norm(got) == norm(truth)


def fuzzy_equal(f: str, got, truth) -> bool:
    if f in AMOUNTS or f in ("invoice_date", "currency") or truth is None or got is None:
        return field_equal(f, got, truth)
    return difflib.SequenceMatcher(None, norm(got), norm(truth)).ratio() >= 0.9


def item_key(li: dict):
    q = lambda v: None if v is None else Decimal(str(v))
    return (norm(li.get("description") or ""), q(li.get("quantity")), q(li.get("unit_price")), q(li.get("amount")))


def match_invoices(truths: list[dict], got: list[dict]):
    """Pair extracted invoices with labeled ones: by invoice number first, then by order."""
    by_num = {norm(g["data"].get("invoice_number") or ""): g for g in got if g["data"].get("invoice_number")}
    used, pairs = set(), []
    for i, t in enumerate(truths):
        g = by_num.get(norm(t["invoice_number"])) if t["invoice_number"] else None
        if g is not None:
            used.add(id(g))
        pairs.append([t, g])
    leftovers = [g for g in got if id(g) not in used]
    for pair in pairs:
        if pair[1] is None and leftovers:
            pair[1] = leftovers.pop(0)
    return pairs, len(leftovers)


def pct(a: int, b: int) -> float | None:
    return round(100 * a / b, 1) if b else None


def percentile(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p * (len(xs) - 1))))] if xs else 0.0


def evaluate(entries: list[dict], workdir: Path, srcdir: Path) -> dict:
    svc = make_services(workdir)
    doc_ids: dict[str, str] = {}
    wall: dict[str, float] = {}
    t_all = time.perf_counter()
    for e in entries:  # sequential: deterministic ordering for duplicate detection
        did = ingest(svc, srcdir / e["file"])
        doc_ids[e["file"]] = did
        t0 = time.perf_counter()
        run_document(svc, did)
        wall[e["file"]] = time.perf_counter() - t0
    total_wall = time.perf_counter() - t_all

    per_cat = defaultdict(lambda: {"fields": Counter(), "n_fields": Counter(), "inv_total": 0, "inv_found": 0,
                                   "inv_perfect": 0, "inv_wrong_flagged": 0, "inv_wrong": 0,
                                   "items_tp": 0, "items_truth": 0, "items_got": 0, "extra": 0})
    overall = per_cat["ALL"]
    fuzzy = Counter()
    invented = Counter()
    docs_out = []
    val = {"inject_total": Counter(), "inject_hit": Counter(), "inject_flagged": Counter(), "clean_total": 0, "clean_flagged": 0, "clean_err_flagged": 0,
           "clean_reasons": Counter(), "clean_total_digital": 0, "clean_flagged_digital": 0}
    fail_expect = {"total": 0, "ok": 0, "details": []}
    valid_docs = valid_ok = 0
    times_by_cat = defaultdict(list)
    ocr_pages = ocr_time = 0
    silent_wrong = []
    code_for_inject = {"total_mismatch": "TOTAL_MISMATCH", "line_math": "LINE_ITEM_MATH", "missing_number": "MISSING_FIELD",
                       "duplicate": "DUPLICATE_INVOICE"}

    with svc.session_factory() as s:
        for e in entries:
            doc = s.get(Document, doc_ids[e["file"]])
            invs = list(s.scalars(select(Invoice).where(Invoice.document_id == doc.id).order_by(Invoice.segment_index)))
            got = [{"data": i.data, "needs_review": i.needs_review, "codes": {x.code for x in i.issues},
                    "errors": {x.code for x in i.issues if x.severity == "error"}, "id": i.id} for i in invs]
            docs_out.append({"file": e["file"], "category": e["category"], "status": doc.status, "error_code": doc.error_code,
                             "seconds": round(wall[e["file"]], 3), "pages": doc.page_count, "invoices": len(invs),
                             "ocr_conf": doc.avg_ocr_confidence})
            if e["expect_fail"]:
                fail_expect["total"] += 1
                ok = doc.status == "failed" and doc.error_code == e["expect_fail"]
                fail_expect["ok"] += ok
                fail_expect["details"].append({"file": e["file"], "expected": e["expect_fail"], "got": doc.error_code or doc.status})
                continue
            valid_docs += 1
            valid_ok += doc.status == "completed"
            times_by_cat[e["category"]].append(wall[e["file"]])
            if doc.ocr_pages:
                ocr_pages += doc.ocr_pages
                ocr_time += wall[e["file"]]
            if doc.status != "completed":
                continue
            pairs, extra = match_invoices(e["invoices"], got)
            for cat in (e["category"], "ALL"):
                per_cat[cat]["extra"] += extra
            for t, g in pairs:
                for cat in (e["category"], "ALL"):
                    per_cat[cat]["inv_total"] += 1
                    per_cat[cat]["items_truth"] += len(t["line_items"])
                if g is None:
                    for cat in (e["category"], "ALL"):
                        per_cat[cat]["items_truth"] += 0
                    continue
                d = g["data"]
                wrong = []
                for f in FIELDS:
                    ok = field_equal(f, d.get(f), t[f])
                    for cat in (e["category"], "ALL"):
                        per_cat[cat]["n_fields"][f] += 1
                        per_cat[cat]["fields"][f] += ok
                    fuzzy[f] += fuzzy_equal(f, d.get(f), t[f])
                    if not ok:
                        wrong.append(f)
                        if t[f] is None and d.get(f) is not None:
                            invented[f] += 1
                tk = Counter(item_key(x) for x in t["line_items"])
                gk = Counter(item_key(x) for x in d.get("line_items") or [])
                tp = sum((tk & gk).values())
                items_perfect = tk == gk
                for cat in (e["category"], "ALL"):
                    per_cat[cat]["inv_found"] += 1
                    per_cat[cat]["items_tp"] += tp
                    per_cat[cat]["items_got"] += sum(gk.values())
                perfect = not wrong and items_perfect
                if perfect:
                    for cat in (e["category"], "ALL"):
                        per_cat[cat]["inv_perfect"] += 1
                else:
                    for cat in (e["category"], "ALL"):
                        per_cat[cat]["inv_wrong"] += 1
                        per_cat[cat]["inv_wrong_flagged"] += g["needs_review"]
                    if not g["needs_review"]:
                        silent_wrong.append({"file": e["file"], "wrong_fields": wrong, "items_ok": items_perfect})
                # validation behaviour
                injected = t["expected_codes"]
                is_dup = "DUPLICATE_INVOICE" in injected
                if injected:
                    key = {"TOTAL_MISMATCH": "total_mismatch", "LINE_ITEM_MATH": "line_math", "MISSING_FIELD": "missing_number",
                           "DUPLICATE_INVOICE": "duplicate"}[injected[0]]
                    val["inject_total"][key] += 1
                    val["inject_hit"][key] += all(c in g["codes"] for c in injected)
                    val["inject_flagged"][key] += bool(g["needs_review"])
                else:
                    val["clean_total"] += 1
                    is_digital = e["category"].startswith(("digital", "xlsx", "xls", "csv"))
                    val["clean_total_digital"] += is_digital
                    if g["needs_review"]:
                        val["clean_flagged"] += 1
                        val["clean_flagged_digital"] += is_digital
                        val["clean_reasons"][",".join(sorted(g["codes"]))[:80]] += 1
                    val["clean_err_flagged"] += bool(g["errors"])

    # stats
    def cat_row(c):
        v = per_cat[c]
        return {
            "invoices_labeled": v["inv_total"], "invoices_found": v["inv_found"],
            "invoice_recall_pct": pct(v["inv_found"], v["inv_total"]),
            "fully_correct_pct": pct(v["inv_perfect"], v["inv_total"]),
            "field_accuracy_pct": {f: pct(v["fields"][f], v["n_fields"][f]) for f in FIELDS},
            "all_fields_micro_pct": pct(sum(v["fields"].values()), sum(v["n_fields"].values())),
            "line_item_precision_pct": pct(v["items_tp"], v["items_got"]),
            "line_item_recall_pct": pct(v["items_tp"], v["items_truth"]),
            "wrong_but_flagged_pct": pct(v["inv_wrong_flagged"], v["inv_wrong"]),
            "wrong_invoices": v["inv_wrong"], "spurious_extra_records": v["extra"],
        }

    cats = [c for c in per_cat if c != "ALL"]
    times_all = [t for ts in times_by_cat.values() for t in ts]
    results = {
        "dataset": {"files": len(entries), "valid_files": valid_docs, "expected_failure_files": fail_expect["total"],
                    "invoices_labeled": overall["inv_total"]},
        "document_success_rate_pct": pct(valid_ok, valid_docs),
        "document_failure_rate_pct": pct(valid_docs - valid_ok, valid_docs),
        "failure_handling": {"expected_failures": fail_expect["total"], "handled_with_correct_error_code": fail_expect["ok"],
                             "details": fail_expect["details"]},
        "accuracy_overall": cat_row("ALL"),
        "accuracy_by_category": {c: cat_row(c) for c in sorted(cats)},
        "fuzzy_field_accuracy_pct": {f: pct(fuzzy[f], overall["n_fields"][f]) for f in FIELDS},
        "invented_values": dict(invented),
        "silent_wrong_invoices": silent_wrong,
        "validation": {
            "injected_error_detection_pct": {k: pct(val["inject_hit"][k], val["inject_total"][k]) for k in val["inject_total"]},
            "injected_flagged_for_review_pct": {k: pct(val["inject_flagged"][k], val["inject_total"][k]) for k in val["inject_total"]},
            "injected_counts": dict(val["inject_total"]),
            "clean_invoices": val["clean_total"],
            "clean_flagged_for_review_pct": pct(val["clean_flagged"], val["clean_total"]),
            "clean_flagged_for_review_digital_pct": pct(val["clean_flagged_digital"], val["clean_total_digital"]),
            "clean_with_error_severity_pct": pct(val["clean_err_flagged"], val["clean_total"]),
            "clean_flag_reasons": val["clean_reasons"].most_common(8),
        },
        "timing": {
            "total_seconds": round(total_wall, 1), "mean_s": round(statistics.mean(times_all), 3),
            "median_s": round(statistics.median(times_all), 3), "p95_s": round(percentile(times_all, 0.95), 3),
            "by_category_mean_s": {c: round(statistics.mean(v), 3) for c, v in sorted(times_by_cat.items())},
            "ocr_pages": ocr_pages, "seconds_per_ocr_page_incl_extraction": round(ocr_time / ocr_pages, 3) if ocr_pages else None,
        },
        "documents": docs_out,
    }
    return results


def stress_digital(pages: int, base: Path) -> dict:
    pdf = base / f"stress_{pages}.pdf"
    t0 = time.perf_counter()
    truths = dataset.build_stress_pdf(pdf, pages)
    gen_s = time.perf_counter() - t0
    svc = make_services(base / "stress_db")
    did = ingest(svc, pdf)
    rss0 = peak_rss_mb()
    t0 = time.perf_counter()
    run_document(svc, did)
    dt = time.perf_counter() - t0
    with svc.session_factory() as s:
        doc = s.get(Document, did)
        invs = list(s.scalars(select(Invoice).where(Invoice.document_id == did)))
    by_num = {i.invoice_number: i for i in invs}
    ok = sum(1 for t in truths if (g := by_num.get(t["invoice_number"])) and
             all(field_equal(f, g.data.get(f), t[f]) for f in FIELDS))
    return {"pages": pages, "pdf_mb": round(pdf.stat().st_size / 1e6, 2), "status": doc.status, "seconds": round(dt, 1),
            "pages_per_second": round(pages / dt, 1), "invoices_found": len(invs),
            "invoices_all_fields_correct_pct": pct(ok, len(truths)),
            "flagged_for_review": sum(i.needs_review for i in invs),
            "peak_rss_mb": peak_rss_mb(), "rss_before_mb": rss0, "generation_seconds": round(gen_s, 1)}


def stress_scan(pages: int, base: Path, workers: int) -> dict:
    import random
    rng = random.Random(5)
    specs = []
    for n in range(pages):
        s = synth.make_spec(rng, n, template=1 + n % 3, n_items=rng.randint(3, 8), money_style="symbol", currency="GBP")
        s.invoice_number = f"SCN-{n + 1:05d}"
        specs.append(s)
    src, pdf = base / "scan_src.pdf", base / f"stress_scan_{pages}.pdf"
    synth.render_pdf(specs, src)
    synth.save_scan(src, pdf, "moderate", rng)
    svc = make_services(base / f"scan_db_{workers}", ocr_workers=workers)
    did = ingest(svc, pdf)
    t0 = time.perf_counter()
    run_document(svc, did)
    dt = time.perf_counter() - t0
    with svc.session_factory() as s:
        doc = s.get(Document, did)
        invs = list(s.scalars(select(Invoice).where(Invoice.document_id == did)))
    by_num = {i.invoice_number: i for i in invs}
    tot_ok = sum(1 for sp in specs if (g := by_num.get(sp.invoice_number)) and g.data.get("total") == str(sp.total))
    return {"pages": pages, "ocr_workers": workers, "status": doc.status, "seconds": round(dt, 1),
            "seconds_per_page": round(dt / pages, 3), "invoices_found": len(invs),
            "invoice_number_and_total_correct_pct": pct(tot_ok, pages), "pdf_mb": round(pdf.stat().st_size / 1e6, 1)}


def render_report(r: dict, stress: dict, args) -> str:
    a = r["accuracy_overall"]
    L = []
    w = L.append
    w("# Evaluation report\n")
    w(f"_Generated by `python -m eval.run_eval --seed {args.seed}`. All numbers below were measured on this machine "
      f"({platform.platform()}, Python {platform.python_version()}, {__import__('os').cpu_count()} CPU)._\n")
    w("## What was measured, and what it is not\n")
    d = r["dataset"]
    w(f"- **Dataset**: {d['files']} files ({d['valid_files']} valid inputs + {d['expected_failure_files']} deliberately broken), "
      f"{d['invoices_labeled']} labeled invoices. Formats: digital PDF, scanned PDF (clean/moderate/poor), PNG, JPEG, TIFF, XLSX, XLS, CSV.")
    w("- **Synthetic, not real-world.** Documents are generated from three invoice templates plus spreadsheet/CSV layouts, with exact labels. "
      "Real vendor invoices have many more layouts, so expect lower numbers on them. Treat these figures as a regression baseline "
      "and a measure of robustness to scan quality, *not* as a production accuracy claim.")
    if LLM:
        w(f"- **Rules plus AI extraction** (`{LLM.model}`, effort `{LLM.effort}`). The LLM only fills fields the rules left weak.\n")
    else:
        w("- **Rule-based extraction only** (no LLM key was used). The LLM path is unit-tested with mocked responses; its accuracy was not measured.\n")
    w("## Headline numbers\n")
    w("| Metric | Result |\n|---|---|")
    w(f"| Document success rate (valid inputs processed to completion) | **{r['document_success_rate_pct']}%** |")
    w(f"| Document failure rate (valid inputs) | **{r['document_failure_rate_pct']}%** |")
    fh = r["failure_handling"]
    w(f"| Broken inputs failed cleanly with the right error code | {fh['handled_with_correct_error_code']}/{fh['expected_failures']} |")
    w(f"| Invoice recall (labeled invoices that produced a record) | {a['invoice_recall_pct']}% |")
    w(f"| Invoices with every field and every line item exactly right | **{a['fully_correct_pct']}%** |")
    w(f"| Field accuracy, all 8 fields pooled (exact match) | **{a['all_fields_micro_pct']}%** |")
    w(f"| Line items: precision / recall | {a['line_item_precision_pct']}% / {a['line_item_recall_pct']}% |")
    w(f"| Wrongly-extracted invoices that were flagged for manual review | **{a['wrong_but_flagged_pct']}%** ({a['wrong_invoices']} wrong) |")
    w(f"| Values invented where the document has none | {sum(r['invented_values'].values())} |")
    t = r["timing"]
    w(f"| Processing time per document: mean / median / p95 | {t['mean_s']}s / {t['median_s']}s / {t['p95_s']}s |")
    w("")
    w("## Field accuracy (exact match)\n")
    w("| Field | Exact | Lenient (≥90% text similarity) |\n|---|---|---|")
    for f in dataset_fields():
        w(f"| {f} | {a['field_accuracy_pct'][f]}% | {r['fuzzy_field_accuracy_pct'][f]}% |")
    w("")
    w("## Accuracy by input type\n")
    w("| Category | Invoices | Fully correct | Fields exact | Line-item P / R | Wrong → flagged | Avg time/doc |\n|---|---|---|---|---|---|---|")
    for c, v in r["accuracy_by_category"].items():
        w(f"| {c} | {v['invoices_labeled']} | {v['fully_correct_pct']}% | {v['all_fields_micro_pct']}% | "
          f"{v['line_item_precision_pct']}% / {v['line_item_recall_pct']}% | "
          f"{v['wrong_but_flagged_pct'] if v['wrong_invoices'] else '–'}{'%' if v['wrong_invoices'] else ''} ({v['wrong_invoices']}) | "
          f"{t['by_category_mean_s'].get(c, '–')}s |")
    w("")
    v = r["validation"]
    w("## Validation behaviour\n")
    w("Injected errors are printed on the document on purpose; the validator must catch them.\n")
    w("`Exact code` = the expected validation code was raised; `Sent to review` = the invoice was flagged for manual review for any reason "
      "(e.g. an OCR-dropped line item turns a LINE_ITEM_MATH into a SUBTOTAL_MISMATCH, which still reaches a human).\n")
    w("| Injected error | Count | Exact code | Sent to review |\n|---|---|---|---|")
    for k, n in v["injected_counts"].items():
        w(f"| {k} | {n} | {v['injected_error_detection_pct'][k]}% | {v['injected_flagged_for_review_pct'][k]}% |")
    w(f"\nOf {v['clean_invoices']} invoices with no injected problem, {v['clean_flagged_for_review_pct']}% were flagged for review "
      f"({v['clean_flagged_for_review_digital_pct']}% among digital/spreadsheet sources) and {v['clean_with_error_severity_pct']}% carried an "
      f"error-severity issue. Flags on clean documents come from ambiguous `dd/mm/yyyy` dates, low-confidence heuristics and OCR damage:\n")
    for reason, n in v["clean_flag_reasons"]:
        w(f"- {n} × `{reason or '(no issue codes)'}`")
    w("")
    if r["silent_wrong_invoices"]:
        w("## Silent errors (wrong and *not* flagged), the most important failures\n")
        for s_ in r["silent_wrong_invoices"][:15]:
            w(f"- `{s_['file']}`: wrong fields {s_['wrong_fields'] or '–'}, line items correct: {s_['items_ok']}")
        w("")
    w("## Failure handling\n")
    w("| File | Expected error | Got |\n|---|---|---|")
    for x in fh["details"]:
        w(f"| {x['file']} | {x['expected']} | {x['got']} |")
    w("")
    w("## Performance and scale\n")
    w(f"- Whole dataset ({d['files']} files) processed sequentially in **{t['total_seconds']}s**.")
    if t["seconds_per_ocr_page_incl_extraction"]:
        w(f"- OCR documents: {t['ocr_pages']} pages OCR'd, ≈ **{t['seconds_per_ocr_page_incl_extraction']}s per page** end-to-end per document (single document at a time).")
    sd = stress.get("digital")
    if sd:
        w(f"- **{sd['pages']}-page digital PDF** ({sd['pdf_mb']} MB, one invoice per page): status `{sd['status']}` in **{sd['seconds']}s** "
          f"({sd['pages_per_second']} pages/s), {sd['invoices_found']} invoices found, {sd['invoices_all_fields_correct_pct']}% with all 8 fields exactly right, "
          f"{sd['flagged_for_review']} flagged for review"
          + (f", peak RSS {sd['peak_rss_mb']} MB." if sd["peak_rss_mb"] else "."))
    for ss in stress.get("scan", []):
        w(f"- **{ss['pages']}-page scanned PDF** ({ss['pdf_mb']} MB, moderate degradation) with {ss['ocr_workers']} OCR worker(s): `{ss['status']}` in "
          f"**{ss['seconds']}s** ({ss['seconds_per_page']}s/page), {ss['invoices_found']} invoices, invoice number + total correct on {ss['invoice_number_and_total_correct_pct']}%.")
    if stress.get("scan"):
        best = min(stress["scan"], key=lambda x: x["seconds_per_page"])
        w(f"- _Extrapolation, not measured:_ at {best['seconds_per_page']}s/page a fully scanned 1000-page PDF would take ≈ {round(best['seconds_per_page'] * 1000 / 60, 1)} minutes on this machine. Digital PDFs avoid OCR entirely.")
    w("")
    w("## Reproduce\n```\npip install -r requirements.txt   # + tesseract-ocr\npython -m eval.run_eval --seed 7 --stress-digital 1000 --stress-scan 40\n```\n")
    return "\n".join(L)


def dataset_fields():
    return FIELDS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--stress-digital", type=int, default=0)
    ap.add_argument("--stress-scan", type=int, default=0)
    ap.add_argument("--keep", type=Path, help="keep generated dataset in this directory")
    ap.add_argument("--llm", action="store_true", help="enable AI extraction (local Ollama)")
    args = ap.parse_args()

    global LLM
    if args.llm:
        LLM = make_llm(replace(Settings.from_env(), llm_enabled=True))

    base = args.keep or Path(tempfile.mkdtemp(prefix="docintel-eval-"))
    base.mkdir(parents=True, exist_ok=True)
    src = base / "files"
    print(f"building dataset in {src} ...", flush=True)
    entries = dataset.build(src, seed=args.seed)
    print(f"{len(entries)} files; evaluating ...", flush=True)
    results = evaluate(entries, base / "run", src)
    stress: dict = {}
    if args.stress_digital:
        print(f"stress: {args.stress_digital}-page digital PDF ...", flush=True)
        stress["digital"] = stress_digital(args.stress_digital, base)
    if args.stress_scan:
        stress["scan"] = []
        for workers in (1, 4):
            print(f"stress: {args.stress_scan}-page scanned PDF, {workers} OCR worker(s) ...", flush=True)
            stress["scan"].append(stress_scan(args.stress_scan, base, workers))
    results["stress"] = stress
    out_dir = Path(__file__).resolve().parent
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, default=str))
    (out_dir / "report.md").write_text(render_report(results, stress, args))
    print((out_dir / "report.md").read_text())
    if not args.keep:
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    main()
