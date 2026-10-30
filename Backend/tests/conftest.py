from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from eval import synth  # noqa: E402


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data", retry_backoff_s=0.0, ocr_workers=2, job_workers=2, max_upload_mb=5)


@pytest.fixture()
def client(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        c.app_state = app.state
        yield c


@pytest.fixture()
def rng() -> random.Random:
    return random.Random(1234)


@pytest.fixture()
def work(tmp_path) -> Path:
    p = tmp_path / "files"
    p.mkdir()
    return p


@pytest.fixture()
def make_pdf(work, rng):
    def _make(name="inv.pdf", **kw) -> tuple[Path, synth.InvoiceSpec]:
        spec = synth.make_spec(rng, 1, **kw)
        path = work / name
        synth.render_pdf([spec], path)
        return path, spec

    return _make


def upload(client: TestClient, *paths: Path, headers=None):
    files = [("files", (p.name, p.read_bytes(), "application/octet-stream")) for p in paths]
    return client.post("/api/documents", files=files, headers=headers or {})


def wait_done(client: TestClient, doc_ids, timeout: float = 90.0, headers=None) -> dict:
    ids = [doc_ids] if isinstance(doc_ids, str) else list(doc_ids)
    deadline = time.time() + timeout
    out: dict = {}
    while time.time() < deadline:
        out = {i: client.get(f"/api/documents/{i}", headers=headers or {}).json() for i in ids}
        if all(d["status"] in ("completed", "failed") for d in out.values()):
            return out if len(ids) > 1 else out[ids[0]]
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {ids}: {[d.get('status') for d in out.values()]}")
