"""Browser end-to-end smoke test (not collected by pytest). Needs `playwright install chromium` and
`npm install` in ../Frontend.

    python tests/e2e_ui.py [--shots DIR]
Builds and starts the real Next.js app against the real API on free ports, drives the UI like a user
and asserts on what is rendered.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT.parent / "Frontend"
sys.path.insert(0, str(ROOT))
from eval import synth  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.25)
    raise TimeoutError(f"nothing listening on port {port}")


def main(shots: Path | None) -> int:
    from playwright.sync_api import expect, sync_playwright

    tmp = Path(tempfile.mkdtemp())
    rng = random.Random(21)
    clean = synth.make_spec(rng, 1, template=1, currency="GBP", money_style="symbol")
    bad = synth.make_spec(rng, 2, template=2, currency="GBP", inject="total_mismatch")
    bad.supplier = "Quokka Plumbing Services"
    files = []
    for name, spec in (("clean.pdf", clean), ("bad_total.pdf", bad)):
        synth.render_pdf([spec], tmp / name)
        files.append(tmp / name)
    reg = [synth.make_spec(rng, n, template=3) for n in range(3)]
    synth.write_xlsx_register(reg, tmp / "register.xlsx")
    files.append(tmp / "register.xlsx")
    (tmp / "broken.pdf").write_bytes(b"%PDF-1.4\n" + b"\x00garbage" * 40)
    files.append(tmp / "broken.pdf")

    api_port, ui_port = free_port(), free_port()
    api_env = {**os.environ, "DATA_DIR": str(tmp / "data"), "RETRY_BACKOFF_S": "0"}
    api_env.pop("LLM_ENABLED", None)
    ui_env = {**os.environ, "BACKEND_URL": f"http://127.0.0.1:{api_port}", "NEXT_TELEMETRY_DISABLED": "1"}
    subprocess.run([shutil.which("npm"), "run", "build"], cwd=FRONTEND, env=ui_env, check=True, stdout=subprocess.DEVNULL)
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(api_port),
                               "--log-level", "warning"], cwd=ROOT, env=api_env)
    ui = subprocess.Popen(["node", "node_modules/next/dist/bin/next", "start", "-p", str(ui_port)], cwd=FRONTEND, env=ui_env,
                          stdout=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{ui_port}"
    try:
        wait_for_port(api_port)
        wait_for_port(ui_port)
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1280, "height": 900})
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base)
            expect(page.locator("h1")).to_have_text("Document Intelligence")

            page.set_input_files("#file", [str(f) for f in files])
            expect(page.locator("#docList .doc")).to_have_count(4, timeout=20000)
            # wait until nothing is queued/processing
            page.wait_for_function("() => document.querySelectorAll('#docList .doc').length === 4 && !/queued|processing/.test(document.querySelector('#docList').textContent)", timeout=90000)
            page.wait_for_timeout(500)
            statuses = [t.strip() for t in page.locator("#docList .doc .badge:not(.warn)").all_inner_texts()]
            assert statuses.count("completed") == 3 and statuses.count("failed") == 1, statuses
            if shots:
                page.screenshot(path=str(shots / "01_documents.png"))

            # open the invoice with a wrong total and fix it
            page.locator("#docList .doc", has_text="bad_total.pdf").click()
            expect(page.locator("#detail .card h3", has_text="Invoice")).to_be_visible()
            expect(page.locator("#detail .issues .error", has_text="TOTAL_MISMATCH")).to_be_visible()
            if shots:
                page.screenshot(path=str(shots / "02_invoice_with_error.png"))
            total_input = page.locator("#detail input[id$='-total']")
            assert total_input.input_value() == str(bad.total)
            total_input.fill(str(bad.subtotal + bad.tax))
            page.get_by_role("button", name="Save corrections").first.click()
            expect(page.locator("#detail .issues .error", has_text="TOTAL_MISMATCH")).to_have_count(0, timeout=10000)
            expect(page.locator("#detail .badge.ok", has_text="corrected").first).to_be_visible()

            # failed document shows its error + reprocess
            page.locator("#docList .doc", has_text="broken.pdf").click()
            expect(page.locator("#detail .issues .error")).to_contain_text("corrupted_file")

            # review queue shrinks, search works
            page.get_by_role("link", name="Review queue").click()
            expect(page.locator("#reviewList")).to_be_visible()
            page.wait_for_timeout(500)
            page.get_by_role("link", name="Search").click()
            page.fill("#searchForm input[name=q]", "quokka plumbing")
            page.get_by_role("button", name="Search", exact=True).click()
            expect(page.locator("#searchResults .result").first).to_contain_text("Quokka Plumbing Services", timeout=10000)
            if shots:
                page.screenshot(path=str(shots / "03_search.png"))

            # a search result opens its document; a reload keeps it open (selection lives in the URL)
            page.locator("#searchResults .result").first.click()
            expect(page.locator("#detail .card h3", has_text="bad_total.pdf")).to_be_visible(timeout=10000)
            page.reload()
            expect(page.locator("#detail .card h3", has_text="bad_total.pdf")).to_be_visible(timeout=10000)

            page.get_by_role("link", name="Cross-check").click()
            expect(page.locator("#reconcileList")).to_contain_text("No cross-document matches found", timeout=10000)
            errors = [e for e in errors if "favicon" not in e]
            assert not errors, errors
            b.close()
        print("UI end-to-end: OK")
        return 0
    finally:
        ui.terminate()
        server.terminate()
        ui.wait(10)
        server.wait(10)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--shots", type=Path)
    a = ap.parse_args()
    if a.shots:
        a.shots.mkdir(parents=True, exist_ok=True)
    sys.exit(main(a.shots))
