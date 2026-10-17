import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal, Optional

from fastapi import Path as PathParam
from pydantic import BaseModel, Field, computed_field

from app.documents import DocumentMetadata
from app.generate import Citation, Strategy

# Only files under this directory may be ingested; the server must not read arbitrary paths.
INGEST_ROOT = Path(os.getenv("INGEST_ROOT", Path(__file__).resolve().parents[2] / "data")).resolve()

TenantId = Annotated[str, PathParam(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$", examples=["acme"])]
JobStatus = Literal["queued", "running", "succeeded", "completed_with_errors", "failed"]


class JobCreated(BaseModel):
    job_id: str
    status: JobStatus
    status_url: str


# --- ingestion ---------------------------------------------------------------

class FileToIngest(BaseModel):
    path: str = Field(description=f"Local .pdf or .md path, must be under {INGEST_ROOT}",
                      examples=["data/pdfs/uber_2021.pdf"])
    metadata: Optional[DocumentMetadata] = None   # omitted: detected from the file


class CreateIngestionJobRequest(BaseModel):
    files: list[FileToIngest] = Field(min_length=1, max_length=100)


class FileFailure(BaseModel):
    path: str
    error: str


class IngestionJob(BaseModel):
    job_id: str
    tenant_id: str
    status: JobStatus
    files_total: int
    files_done: int = 0
    files_failed: int = 0
    failures: list[FileFailure] = []
    error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    @computed_field
    @property
    def files_remaining(self) -> int:
        return self.files_total - self.files_done - self.files_failed


# --- queries -----------------------------------------------------------------

class QueryFilters(BaseModel):
    ticker: Optional[str] = Field(None, examples=["UBER"])
    fiscal_year: Optional[int] = Field(None, ge=1990, le=2100, examples=[2021])
    doc_ids: Optional[list[str]] = Field(None, max_length=100, description="Restrict to these filings",
                                         examples=[["uber_2021"]])


class CreateQueryJobRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000, examples=["What was Uber's total revenue in 2021?"])
    filters: QueryFilters = QueryFilters()
    strategy: Strategy = Field("auto", description="hybrid | graph (hybrid + context graph) | agent (ReAct over tools) | auto")


class QueryJob(BaseModel):
    job_id: str
    tenant_id: str
    status: JobStatus
    question: str
    filters: QueryFilters
    strategy: Strategy = "auto"             # requested
    strategy_used: Optional[str] = None     # resolved (auto -> hybrid/graph)
    steps: list[dict] = []                  # agent: think / tool / observe trace, updated live while running
    answer: Optional[str] = None
    citations: list[Citation] = []
    error: Optional[str] = None
    created_at: datetime
    finished_at: Optional[datetime] = None


# --- health ------------------------------------------------------------------

class Health(BaseModel):
    status: Literal["ok", "degraded"]
    checks: dict[str, str]   # component -> "ok" or the error
