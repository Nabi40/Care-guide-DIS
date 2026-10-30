"""Runtime configuration, read from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    database_url: str = ""  # defaults to sqlite file in data_dir
    api_key: str | None = None  # if set, every /api request needs X-API-Key

    # Upload / safety limits
    max_upload_mb: int = 50
    max_files_per_request: int = 50
    max_pdf_pages: int = 3000
    max_sheet_rows: int = 200_000
    max_image_pixels: int = 120_000_000

    # OCR
    ocr_enabled: bool = True
    ocr_lang: str = "eng"
    ocr_dpi: int = 200
    ocr_workers: int = 4
    ocr_timeout_s: int = 90
    min_text_chars_per_page: int = 30  # below this a PDF page is treated as scanned

    # Jobs
    job_workers: int = 2
    max_attempts: int = 3
    retry_backoff_s: float = 1.0

    # Extraction / validation
    date_dayfirst: bool = True  # UK / BD convention for 05/03/2026
    low_confidence: float = 0.6
    amount_tolerance: Decimal = Decimal("0.02")
    line_tolerance: Decimal = Decimal("0.01")

    # LLM (optional, local via Ollama). When disabled the rule-based extractor is used alone.
    llm_enabled: bool = False
    llm_model: str = "qwen2.5:7b"  # Apache-2.0; fits a 6 GB GPU
    llm_base_url: str = "http://127.0.0.1:11434"
    llm_timeout_s: int = 300
    llm_num_ctx: int = 16384
    llm_max_chars: int = 40_000
    llm_workers: int = 1  # one GPU; raise together with OLLAMA_NUM_PARALLEL

    # Search
    embedding_backend: str = "hashing"  # "auto" | "hashing" | "sentence-transformers"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite:///{(self.data_dir / 'docintel.db').resolve()}"

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", "data"))
        return cls(
            data_dir=data_dir,
            database_url=os.getenv("DATABASE_URL", ""),
            api_key=os.getenv("API_KEY") or None,
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 50),
            max_files_per_request=_env_int("MAX_FILES_PER_REQUEST", 50),
            max_pdf_pages=_env_int("MAX_PDF_PAGES", 3000),
            max_sheet_rows=_env_int("MAX_SHEET_ROWS", 200_000),
            ocr_enabled=_env_bool("OCR_ENABLED", True),
            ocr_lang=os.getenv("OCR_LANG", "eng"),
            ocr_dpi=_env_int("OCR_DPI", 200),
            ocr_workers=_env_int("OCR_WORKERS", min(4, os.cpu_count() or 2)),
            job_workers=_env_int("JOB_WORKERS", 2),
            max_attempts=_env_int("MAX_ATTEMPTS", 3),
            retry_backoff_s=_env_float("RETRY_BACKOFF_S", 1.0),
            date_dayfirst=_env_bool("DATE_DAYFIRST", True),
            llm_enabled=_env_bool("LLM_ENABLED", False),
            llm_model=os.getenv("LLM_MODEL", "qwen2.5:7b"),
            llm_base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:11434"),
            llm_timeout_s=_env_int("LLM_TIMEOUT_S", 300),
            llm_num_ctx=_env_int("LLM_NUM_CTX", 16384),
            llm_workers=_env_int("LLM_WORKERS", 1),
            embedding_backend=os.getenv("EMBEDDING_BACKEND", "auto"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
        )
