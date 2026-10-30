# Document Intelligence

Upload invoices as PDF, scans, images, Excel or CSV. The system extracts structured data without inventing values, validates it,
flags what needs a human, and lets you review, correct, cross-check and search everything.

| Folder | What it is |
|---|---|
| [`Backend/`](Backend) | FastAPI service: processing pipeline, extraction, validation, search, tests, evaluation. Details in [`Backend/README.md`](Backend/README.md) |
| [`Frontend/`](Frontend) | Next.js web UI |

Architecture and database schema: [`Backend/docs/ARCHITECTURE.md`](Backend/docs/ARCHITECTURE.md). Measured results:
[`Backend/eval/report.md`](Backend/eval/report.md) and [`Backend/eval/search_report.md`](Backend/eval/search_report.md).

## Run it

You need Python 3.12+ and Node.js 20+. Use two terminals.

**Terminal 1: backend** (Windows PowerShell; on Linux/macOS activate with `. web_env/bin/activate`)

```powershell
cd Backend
python -m venv web_env
.\web_env\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:create_app --factory --port 8000
```

**Terminal 2: frontend**

```powershell
cd Frontend
npm install
npm run dev
```

Open **http://localhost:3000**. API docs are at http://localhost:8000/docs.

For production, run `npm run build` and `npm start` instead of `npm run dev`. If the backend is not on `127.0.0.1:8000`, set
`BACKEND_URL` before building (see [`Frontend/.env.example`](Frontend/.env.example)).

## Optional

- **OCR for scans and images:** install [Tesseract](https://github.com/tesseract-ocr/tesseract) and put it on `PATH`.
- **AI extraction (free, runs locally):** install [Ollama](https://ollama.com), run `ollama pull qwen2.5:7b`, then set
  `$env:LLM_ENABLED = "true"` in the backend terminal before starting it. Nothing leaves your machine and there are no API costs.
# Care-guide-DIS
