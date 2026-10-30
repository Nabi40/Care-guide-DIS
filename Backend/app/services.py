"""Service container shared by the API and the background workers."""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

from sqlalchemy.orm import sessionmaker

from .config import Settings
from .extraction.llm import LLMClient, OllamaClient
from .search import Embedder


@dataclass
class Services:
    settings: Settings
    session_factory: sessionmaker
    embedder: Embedder
    llm: LLMClient | None = None
    # serialises "check duplicates + commit" so two documents with the same invoice cannot both pass
    persist_lock: threading.Lock = field(default_factory=threading.Lock)


def make_llm(settings: Settings) -> LLMClient | None:
    if settings.llm_enabled:
        return OllamaClient(settings.llm_model, settings.llm_base_url, settings.llm_timeout_s, settings.llm_num_ctx)
    return None
