"""In-memory job store, worker pools and the background job functions (jobs are lost on restart)."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, status
from pydantic import BaseModel

from app.api.schemas import FileFailure, QueryFilters
from app.documents import Document, DocumentMetadata, detect_metadata
from app.embeddings import release_gpu
from app.generate import answer_question
from app.graph.store import rebuild_graph
from app.store import connect, ingest_file

logger = logging.getLogger(__name__)


def now() -> datetime:
    return datetime.now(timezone.utc)


class JobStore:
    def __init__(self):
        self._jobs: dict[str, BaseModel] = {}
        self._lock = threading.Lock()

    def add(self, job: BaseModel) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, tenant_id: str, job_id: str) -> BaseModel:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.tenant_id != tenant_id:   # don't reveal other tenants' jobs
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"job {job_id} not found")
            return job.model_copy(deep=True)

    def update(self, job_id: str, **fields) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for name, value in fields.items():
                setattr(job, name, value)


ingestion_jobs, query_jobs = JobStore(), JobStore()
pools: dict[str, ThreadPoolExecutor] = {}  # created per app lifespan (a shut-down pool can't be reused)


def start_pools() -> None:
    pools["ingest"] = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ingest")  # one GPU: one ingest at a time
    pools["query"] = ThreadPoolExecutor(max_workers=2, thread_name_prefix="query")    # LLM calls serialize on GPU_LOCK


def stop_pools() -> None:
    for pool in pools.values():
        pool.shutdown(wait=False, cancel_futures=True)
    pools.clear()


def run_ingestion(job_id: str, tenant_id: str, files: list[tuple[Path, DocumentMetadata | None]]) -> None:
    ingestion_jobs.update(job_id, status="running", started_at=now())
    done, failures = 0, []
    try:
        with connect() as conn:
            for path, meta in files:
                try:
                    meta = meta or detect_metadata(path)   # no metadata given: read it off the cover page
                    ingest_file(conn, tenant_id, path, Document(doc_id=path.stem, **meta.model_dump()))
                    done += 1
                    ingestion_jobs.update(job_id, files_done=done)
                except Exception as e:
                    logger.exception("ingestion job %s: %s failed", job_id, path.name)
                    failures.append(FileFailure(path=str(path), error=f"{type(e).__name__}: {e}"))
                    ingestion_jobs.update(job_id, files_failed=len(failures), failures=list(failures))
            if any(p.suffix.lower() == ".md" for p, _ in files) and done:
                rebuild_graph(conn, tenant_id)   # markdown changed: refresh the tenant's context graph
        final = "succeeded" if not failures else "completed_with_errors" if done else "failed"
        ingestion_jobs.update(job_id, status=final, finished_at=now())
    except Exception as e:
        logger.exception("ingestion job %s crashed", job_id)
        ingestion_jobs.update(job_id, status="failed", error=f"{type(e).__name__}: {e}", finished_at=now())
    finally:
        release_gpu()
    logger.info("ingestion job %s finished: %d done, %d failed", job_id, done, len(failures))


def run_query(job_id: str, tenant_id: str, question: str, filters: QueryFilters, strategy: str = "auto") -> None:
    query_jobs.update(job_id, status="running")
    try:
        with connect() as conn:
            result = answer_question(
                conn, tenant_id, question, ticker=filters.ticker, fiscal_year=filters.fiscal_year,
                doc_ids=filters.doc_ids, strategy=strategy,
                on_step=lambda steps: query_jobs.update(job_id, steps=[s.model_dump() for s in steps]))  # live trace
        query_jobs.update(job_id, status="succeeded", answer=result.answer, citations=result.citations,
                          strategy_used=result.strategy, steps=result.steps, finished_at=now())
    except Exception as e:
        logger.exception("query job %s failed", job_id)
        query_jobs.update(job_id, status="failed", error=f"{type(e).__name__}: {e}", finished_at=now())
