"""API endpoint tests: upload, status, results, search, corrections, reprocessing, security."""
from __future__ import annotations

import random

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from eval import synth

from .conftest import upload, wait_done


def test_upload_process_and_fetch_result(client, make_pdf):
    path, spec = make_pdf()
    r = upload(client, path)
    assert r.status_code == 202
    doc_id = r.json()["accepted"][0]["id"]
    doc = wait_done(client, doc_id)
    assert doc["status"] == "completed"
    assert doc["invoice_count"] == 1 and doc["progress"] == 1.0
    inv = doc["invoices"][0]
    assert inv["data"]["invoice_number"] == spec.invoice_number
    assert inv["data"]["total"] == str(spec.total)
    assert inv["needs_review"] is False and inv["issue_counts"]["error"] == 0
    stages = [e["stage"] for e in doc["events"]]
    assert {"load", "extract", "persist"} <= set(stages)  # processing history recorded
    got = client.get(f"/api/invoices/{inv['id']}").json()
    assert got["field_meta"]["total"]["evidence"]


def test_multiple_files_in_one_request(client, make_pdf):
    a, _ = make_pdf("a.pdf")
    b, _ = make_pdf("b.pdf")
    r = upload(client, a, b)
    ids = [x["id"] for x in r.json()["accepted"]]
    assert len(ids) == 2
    docs = wait_done(client, ids)
    assert all(d["status"] == "completed" for d in docs.values())
    assert client.get("/api/documents").json()["total"] == 2


def test_rejects_bad_extension_and_wrong_content(client, work):
    exe = work / "evil.exe"
    exe.write_bytes(b"MZ\x90\x00")
    r = upload(client, exe)
    assert r.status_code == 400 and r.json()["detail"]["rejected"][0]["status"] == 415

    fake = work / "fake.pdf"
    fake.write_bytes(b"<html>not a pdf</html>")
    r = upload(client, fake)
    assert r.status_code == 400 and "PDF" in r.json()["detail"]["rejected"][0]["reason"]

    empty = work / "empty.csv"
    empty.write_bytes(b"")
    assert upload(client, empty).status_code == 400


def test_partial_acceptance(client, make_pdf, work):
    ok, _ = make_pdf()
    bad = work / "x.txt"
    bad.write_text("hello")
    r = upload(client, ok, bad)
    assert r.status_code == 202
    assert len(r.json()["accepted"]) == 1 and len(r.json()["rejected"]) == 1


def test_oversize_upload_rejected(client, work):
    big = work / "big.csv"
    big.write_bytes(b"a,b\n" + b"1,2\n" * (2 * 1024 * 1024))  # > 5 MB limit in fixture
    r = upload(client, big)
    assert r.status_code == 400 and r.json()["detail"]["rejected"][0]["status"] == 413


def test_path_traversal_filename_is_neutralised(client, make_pdf, settings):
    path, _ = make_pdf()
    files = [("files", ("../../etc/passwd.pdf", path.read_bytes(), "application/pdf"))]
    r = client.post("/api/documents", files=files)
    assert r.status_code == 202
    doc = wait_done(client, r.json()["accepted"][0]["id"])
    assert doc["filename"] == "passwd.pdf"
    stored = list(settings.upload_dir.iterdir())
    assert len(stored) == 1 and stored[0].parent == settings.upload_dir


def test_corrupted_file_fails_cleanly_and_is_reprocessable(client, work):
    bad = work / "broken.xlsx"
    bad.write_bytes(b"PK\x03\x04" + b"garbage" * 50)
    doc = wait_done(client, upload(client, bad).json()["accepted"][0]["id"])
    assert doc["status"] == "failed"
    assert doc["error_code"] == "corrupted_file"
    assert doc["attempts"] == 1  # permanent errors are not retried
    assert any(e["status"] == "error" for e in doc["events"])
    # reprocess-failed endpoint re-queues it; it fails again, deterministically
    r = client.post("/api/documents/reprocess-failed")
    assert r.json()["requeued"] == [doc["id"]]
    again = wait_done(client, doc["id"])
    assert again["status"] == "failed"
    assert client.get("/api/stats").json()["documents"]["failed"] == 1


def test_transient_failure_is_retried_then_succeeds(client, make_pdf, monkeypatch):
    import app.pipeline as pipeline

    real = pipeline.load_document
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("temporary glitch")
        return real(*a, **kw)

    monkeypatch.setattr(pipeline, "load_document", flaky)
    path, _ = make_pdf()
    doc = wait_done(client, upload(client, path).json()["accepted"][0]["id"])
    assert doc["status"] == "completed" and doc["attempts"] == 3 and calls["n"] == 3


def test_reprocess_requires_force_for_completed(client, make_pdf):
    path, _ = make_pdf()
    doc = wait_done(client, upload(client, path).json()["accepted"][0]["id"])
    assert client.post(f"/api/documents/{doc['id']}/reprocess").status_code == 409
    assert client.post(f"/api/documents/{doc['id']}/reprocess?force=true").status_code == 202
    again = wait_done(client, doc["id"])
    assert again["status"] == "completed" and again["invoice_count"] == 1


def test_manual_correction_revalidates_and_is_audited(client, make_pdf, rng):
    spec = synth.make_spec(rng, 5, inject="total_mismatch")
    path = make_pdf.__closure__ and None  # placeholder to keep linter quiet
    from pathlib import Path
    p = Path(client.app_state.svc.settings.data_dir) / "tm.pdf"
    synth.render_pdf([spec], p)
    doc = wait_done(client, upload(client, p).json()["accepted"][0]["id"])
    inv = doc["invoices"][0]
    assert inv["needs_review"] is True
    assert "TOTAL_MISMATCH" in {i["code"] for i in inv["issues"]}

    fixed = client.patch(f"/api/invoices/{inv['id']}", json={"total": str(spec.subtotal + spec.tax)})
    assert fixed.status_code == 200
    body = fixed.json()
    assert body["status"] == "corrected"
    assert "TOTAL_MISMATCH" not in {i["code"] for i in body["issues"]}
    assert body["field_meta"]["total"]["source"] == "manual"
    assert body["corrections"][0]["field"] == "total"
    assert body["needs_review"] is False


def test_patch_validation_errors(client, make_pdf):
    path, _ = make_pdf()
    inv = wait_done(client, upload(client, path).json()["accepted"][0]["id"])["invoices"][0]
    assert client.patch(f"/api/invoices/{inv['id']}", json={}).status_code == 400
    assert client.patch(f"/api/invoices/{inv['id']}", json={"total": "not-a-number"}).status_code == 422
    assert client.patch(f"/api/invoices/{inv['id']}", json={"currency": "POUNDS"}).status_code == 422
    assert client.patch(f"/api/invoices/{inv['id']}", json={"bogus": 1}).status_code == 422
    assert client.patch("/api/invoices/nope", json={"total": "1"}).status_code == 404


def test_approve_requires_note_when_errors_remain(client, rng):
    spec = synth.make_spec(rng, 6, inject="line_math")
    from pathlib import Path
    p = Path(client.app_state.svc.settings.data_dir) / "lm.pdf"
    synth.render_pdf([spec], p)
    inv = wait_done(client, upload(client, p).json()["accepted"][0]["id"])["invoices"][0]
    assert client.post(f"/api/invoices/{inv['id']}/approve").status_code == 409
    ok = client.post(f"/api/invoices/{inv['id']}/approve", json={"note": "supplier confirmed by phone"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved" and ok.json()["needs_review"] is False


def test_duplicate_invoice_across_documents_is_flagged(client, rng):
    from pathlib import Path
    base = Path(client.app_state.svc.settings.data_dir)
    s1 = synth.make_spec(rng, 1, template=1)
    s2 = synth.make_spec(rng, 2, template=2)
    s2.invoice_number, s2.supplier = s1.invoice_number, s1.supplier
    p1, p2 = base / "one.pdf", base / "two.pdf"
    synth.render_pdf([s1], p1)
    synth.render_pdf([s2], p2)
    d1 = wait_done(client, upload(client, p1).json()["accepted"][0]["id"])
    d2 = wait_done(client, upload(client, p2).json()["accepted"][0]["id"])
    assert "DUPLICATE_INVOICE" not in {i["code"] for i in d1["invoices"][0]["issues"]}
    assert "DUPLICATE_INVOICE" in {i["code"] for i in d2["invoices"][0]["issues"]}
    rec = client.get("/api/reconcile?only_mismatches=false").json()
    assert rec["count"] == 1 and {r["document_id"] for r in rec["groups"][0]["records"]} == {d1["id"], d2["id"]}


def test_concurrent_duplicates_are_both_not_missed(client, rng):
    """Two identical invoices uploaded at once must not both pass the duplicate check."""
    from pathlib import Path
    base = Path(client.app_state.svc.settings.data_dir)
    s = synth.make_spec(rng, 1, template=3)
    paths = []
    for n in range(4):
        p = base / f"c{n}.pdf"
        synth.render_pdf([s], p)
        paths.append(p)
    ids = [x["id"] for x in upload(client, *paths).json()["accepted"]]
    docs = wait_done(client, ids)
    flagged = sum("DUPLICATE_INVOICE" in {i["code"] for i in d["invoices"][0]["issues"]} for d in docs.values())
    assert flagged == 3  # exactly one original, three duplicates


def test_search_filters_keyword_and_semantic(client, rng):
    from pathlib import Path
    base = Path(client.app_state.svc.settings.data_dir)
    specs = [synth.make_spec(rng, n, template=1 + n % 3, currency="GBP") for n in range(4)]
    specs[0].supplier = "Zephyr Wind Turbines Ltd"
    specs[1].supplier = "Quokka Plumbing Services"
    paths = []
    for n, sp in enumerate(specs):
        p = base / f"s{n}.pdf"
        synth.render_pdf([sp], p)
        paths.append(p)
    ids = [x["id"] for x in upload(client, *paths).json()["accepted"]]
    wait_done(client, ids)

    kw = client.get("/api/search", params={"q": "zephyr", "mode": "keyword"}).json()
    assert kw["count"] == 1 and kw["items"][0]["data"]["supplier_name"] == "Zephyr Wind Turbines Ltd"
    assert "snippet" in kw["items"][0]

    sem = client.get("/api/search", params={"q": "zephyr wind turbine", "mode": "semantic"}).json()
    assert sem["items"][0]["data"]["supplier_name"] == "Zephyr Wind Turbines Ltd"  # fuzzy match on 'turbine' vs 'Turbines'

    hyb = client.get("/api/search", params={"q": "quokka plumbing", "mode": "hybrid", "currency": "GBP"}).json()
    assert hyb["items"][0]["data"]["supplier_name"] == "Quokka Plumbing Services"

    f = client.get("/api/invoices", params={"supplier": "quokka", "min_total": 1}).json()
    assert f["total"] == 1
    none = client.get("/api/invoices", params={"supplier": "quokka", "min_total": 10**9}).json()
    assert none["total"] == 0
    assert client.get("/api/search", params={"q": "x", "mode": "bogus"}).status_code == 422


def test_search_escapes_like_wildcards(client, make_pdf):
    path, _ = make_pdf()
    wait_done(client, upload(client, path).json()["accepted"][0]["id"])
    # unescaped, 'inv%' / 'i_voice' would match every invoice via SQL LIKE wildcards
    assert client.get("/api/search", params={"q": "inv%", "mode": "keyword"}).json()["count"] == 0
    assert client.get("/api/search", params={"q": "i_voice", "mode": "keyword"}).json()["count"] == 0
    assert client.get("/api/search", params={"q": "invoice", "mode": "keyword"}).json()["count"] == 1


def test_delete_document_removes_file_and_rows(client, make_pdf, settings):
    path, _ = make_pdf()
    doc = wait_done(client, upload(client, path).json()["accepted"][0]["id"])
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 204
    assert client.get(f"/api/documents/{doc['id']}").status_code == 404
    assert client.get("/api/invoices").json()["total"] == 0
    assert list(settings.upload_dir.iterdir()) == []


def test_text_and_file_endpoints(client, make_pdf):
    path, spec = make_pdf()
    doc = wait_done(client, upload(client, path).json()["accepted"][0]["id"])
    text = client.get(f"/api/documents/{doc['id']}/text").json()
    assert spec.supplier in text["pages"][0]["text"] or spec.invoice_number in text["pages"][0]["text"]
    f = client.get(f"/api/documents/{doc['id']}/file")
    assert f.status_code == 200 and f.headers["content-disposition"].startswith("attachment")
    assert f.headers["x-content-type-options"] == "nosniff"


def test_api_key_enforced(tmp_path, make_pdf):
    s = Settings(data_dir=tmp_path / "d2", api_key="s3cret", retry_backoff_s=0.0)
    with TestClient(create_app(s)) as c:
        assert c.get("/api/stats").status_code == 401
        assert c.get("/api/stats", headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.get("/api/stats", headers={"X-API-Key": "s3cret"}).status_code == 200
        assert c.get("/health").status_code == 200
        path, _ = make_pdf()
        h = {"X-API-Key": "s3cret"}
        r = upload(c, path, headers=h)
        assert r.status_code == 202
        wait_done(c, r.json()["accepted"][0]["id"], headers=h)


def test_restart_recovers_unfinished_documents(tmp_path, make_pdf):
    """A document left 'queued' (crash before processing) is picked up on startup."""
    s = Settings(data_dir=tmp_path / "d3", retry_backoff_s=0.0)
    app = create_app(s, start_workers=False)
    with TestClient(app) as c:
        path, _ = make_pdf()
        r = upload(c, path)
        doc_id = r.json()["accepted"][0]["id"]
    # first app instance shut down; start a new one on the same data dir
    with TestClient(create_app(s)) as c2:
        doc = wait_done(c2, doc_id)
        assert doc["status"] == "completed"


def test_security_headers(client):
    r = client.get("/")
    assert r.status_code == 200 and r.json()["docs"] == "/docs"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
