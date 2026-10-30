"""Semantic search behaviour, with a deterministic fake embedder so no model download is needed."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Invoice
from app.main import create_app
from app.search import HashingEmbedder, embedding_text, make_embedder, search_text_body
from eval import synth
from tests.conftest import upload, wait_done


class ConceptEmbedder:
    """Maps words onto a few concepts, the way a real model maps synonyms close together."""
    name = "concepts-v1"
    dim = 3
    min_score = 0.3
    CONCEPTS = (("cleaning", "janitorial", "cleaner"), ("laptop", "computer"), ("audit", "inspection"))

    def embed(self, texts):
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for r, text in enumerate(texts):
            low = text.casefold()
            out[r] = [sum(w in low for w in group) for group in self.CONCEPTS]
            if (n := np.linalg.norm(out[r])) > 0:
                out[r] /= n
        return out


def pdf(client, name, spec) -> str:
    p = Path(client.app_state.svc.settings.data_dir) / name
    synth.render_pdf([spec], p)
    return wait_done(client, upload(client, p).json()["accepted"][0]["id"])["invoices"][0]["id"]


def spec_with(rng, n, description, supplier="Plain Trading Ltd"):
    s = synth.make_spec(rng, n, template=1, n_items=1)
    s.items[0].description, s.supplier = description, supplier
    return s


def semantic_client(settings):
    app = create_app(settings, embedder=ConceptEmbedder())
    c = TestClient(app)
    c.app_state = app.state
    return c


def test_semantic_search_finds_synonyms_and_drops_noise(settings, rng):
    with semantic_client(settings) as c:
        pdf(c, "a.pdf", spec_with(rng, 1, "Cleaning service"))
        pdf(c, "b.pdf", spec_with(rng, 2, "Laptop stand"))
        hits = c.get("/api/search", params={"q": "janitorial", "mode": "semantic"}).json()
        assert [h["data"]["line_items"][0]["description"] for h in hits["items"]] == ["Cleaning service"]
        assert c.get("/api/search", params={"q": "giraffe", "mode": "semantic"}).json()["count"] == 0


def test_hybrid_keeps_exact_matches_with_no_semantic_similarity(settings, rng):
    with semantic_client(settings) as c:
        spec = spec_with(rng, 1, "Laptop stand")
        pdf(c, "a.pdf", spec)
        q = spec.invoice_number  # an identifier: the embedder sees nothing in it
        assert c.get("/api/search", params={"q": q, "mode": "semantic"}).json()["count"] == 0
        hyb = c.get("/api/search", params={"q": q, "mode": "hybrid"}).json()
        assert hyb["count"] == 1 and hyb["items"][0]["data"]["invoice_number"] == q


def test_correction_updates_the_vector(settings, rng):
    with semantic_client(settings) as c:
        inv = pdf(c, "a.pdf", spec_with(rng, 1, "Laptop stand"))
        assert c.get("/api/search", params={"q": "janitorial", "mode": "semantic"}).json()["count"] == 0
        items = [{"description": "Cleaning service", "quantity": None, "unit_price": None, "amount": None}]
        assert c.patch(f"/api/invoices/{inv}", json={"line_items": items}).status_code == 200
        assert c.get("/api/search", params={"q": "janitorial", "mode": "semantic"}).json()["count"] == 1


def test_changing_embedding_model_reindexes_existing_invoices(settings, rng):
    with TestClient(create_app(settings, embedder=HashingEmbedder())) as first:
        first.app_state = first.app.state
        inv = pdf(first, "a.pdf", spec_with(rng, 1, "Cleaning service"))
    with semantic_client(settings) as second:
        svc = second.app_state.svc
        for _ in range(100):
            with svc.session_factory() as s:
                if s.get(Invoice, inv).embedding_model == ConceptEmbedder.name:
                    break
            import time
            time.sleep(0.05)
        hits = second.get("/api/search", params={"q": "janitorial", "mode": "semantic"}).json()
        assert hits["count"] == 1 and hits["embedding_model"] == ConceptEmbedder.name


def test_auto_backend_degrades_to_hashing_when_the_model_cannot_load(monkeypatch):
    import app.search as mod

    def boom(self, model):
        raise OSError("no network")

    monkeypatch.setattr(mod.SentenceTransformerEmbedder, "__init__", boom)
    assert isinstance(make_embedder("auto", "whatever"), HashingEmbedder)


def test_stats_reports_whether_search_is_really_semantic(settings, tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path / "h"))) as c:
        assert c.get("/api/stats").json()["semantic_embeddings"] is False
    with TestClient(create_app(settings, embedder=ConceptEmbedder())) as c:
        assert c.get("/api/stats").json()["semantic_embeddings"] is True


def test_embedding_text_leads_with_meaning_not_numbers():
    text = embedding_text({"supplier_name": "Acme", "customer_name": "Beta", "total": "99.00", "invoice_number": "X-1",
                           "line_items": [{"description": "Toner"}, {"description": None}]}, "body")
    assert text.startswith("Invoice from Acme to Beta. Items: Toner.") and "99.00" not in text and "X-1" not in text


def test_stopwords_do_not_inflate_the_keyword_score():
    from app.search import keyword_score
    assert keyword_score("the invoice for cleaning", "food for the event") == 0.0
    assert keyword_score("the invoice for cleaning", "cleaning for office") == 0.5
    assert keyword_score("for the win", "for the") == 1.0  # a query of only stopwords still works


def test_search_text_body_roundtrip():
    from app.search import invoice_search_text
    assert search_text_body(invoice_search_text({}, "line one\nline two")) == "line one\nline two"
    assert search_text_body("") == ""
