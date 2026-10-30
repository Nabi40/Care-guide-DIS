"""In-process background job queue (thread pool). State lives in the database, so jobs survive restarts.

For horizontal scale replace `JobQueue.submit` with a Redis-backed queue (RQ / Celery / BullMQ);
`pipeline.run_document(svc, doc_id)` is already a self-contained, idempotent unit of work.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select

from .db import Document
from .pipeline import run_document
from .services import Services

log = logging.getLogger(__name__)


class JobQueue:
    def __init__(self, svc: Services):
        self.svc = svc
        self._pool = ThreadPoolExecutor(max_workers=svc.settings.job_workers, thread_name_prefix="docjob")

    def submit(self, doc_id: str) -> None:
        self._pool.submit(run_document, self.svc, doc_id)

    def recover(self) -> int:
        """Re-queue documents left queued/processing by a crash or restart."""
        with self.svc.session_factory() as s:
            ids = list(s.scalars(select(Document.id).where(Document.status.in_(("queued", "processing")))
                                 .order_by(Document.created_at)))
        for i in ids:
            self.submit(i)
        return len(ids)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
