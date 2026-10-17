import logging
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import TypeAdapter, ValidationError

from app.api import jobs
from app.api.schemas import INGEST_ROOT, CreateIngestionJobRequest, IngestionJob, JobCreated, TenantId
from app.documents import DocumentMetadata

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{tenant_id}/ingestion-jobs", tags=["ingestion"])

UPLOAD_DIR = INGEST_ROOT / "uploads"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
SUPPORTED_SUFFIXES = {".pdf", ".md"}
_metadata_list = TypeAdapter(list[DocumentMetadata | None])


def _resolve_pdf(raw: str) -> Path:
    path = Path(raw)
    path = (path if path.is_absolute() else INGEST_ROOT.parent / path).resolve()
    if not path.is_relative_to(INGEST_ROOT):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{raw}: must be under {INGEST_ROOT}")
    if path.suffix.lower() not in SUPPORTED_SUFFIXES or not path.is_file():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{raw}: not an existing .pdf or .md file")
    return path


def _safe_stem(filename: str | None) -> str:
    """Upload name -> doc_id: lowercase, [a-z0-9_-] only, no path components."""
    return re.sub(r"[^a-z0-9_-]+", "_", Path(filename or "").stem.lower()).strip("_") or "document"


def _looks_valid(suffix: str, data: bytes) -> bool:
    if suffix == ".pdf":
        return data.startswith(b"%PDF")
    if suffix == ".md":
        try:
            data.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return False
    return False


def _start_job(tenant_id: str, files: list[tuple[Path, DocumentMetadata]]) -> JobCreated:
    stems = [p.stem for p, _ in files]
    if len(set(stems)) != len(stems):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "file names must be unique: they become doc_ids")
    job = IngestionJob(job_id=uuid.uuid4().hex, tenant_id=tenant_id, status="queued",
                       files_total=len(files), created_at=jobs.now())
    jobs.ingestion_jobs.add(job)
    jobs.pools["ingest"].submit(jobs.run_ingestion, job.job_id, tenant_id, files)
    logger.info("ingestion job %s queued: tenant=%s files=%s", job.job_id, tenant_id, stems)
    return JobCreated(job_id=job.job_id, status=job.status,
                      status_url=f"/tenants/{tenant_id}/ingestion-jobs/{job.job_id}")


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreated,
             summary="Start ingesting server-side PDFs with their filing metadata")
def create_ingestion_job(tenant_id: TenantId, request: CreateIngestionJobRequest) -> JobCreated:
    return _start_job(tenant_id, [(_resolve_pdf(f.path), f.metadata) for f in request.files])


@router.post("/uploads", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreated,
             summary="Upload PDFs and start ingesting them")
async def create_upload_ingestion_job(
    tenant_id: TenantId,
    files: list[UploadFile] = File(..., description="PDF or markdown files"),
    metadata: str | None = Form(None, description="Optional JSON list of filing metadata, one per file in "
                                                  "order; omit it (or use null entries) to detect from the cover page"),
) -> JobCreated:
    try:
        metas = _metadata_list.validate_json(metadata) if metadata else [None] * len(files)
    except ValidationError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"metadata: {e}") from e
    if len(metas) != len(files):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"{len(files)} files but {len(metas)} metadata entries")

    # Validate everything before writing anything.
    payloads = []
    for f in files:
        data = await f.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"{f.filename}: larger than 50 MB")
        suffix = Path(f.filename or "").suffix.lower()
        if not _looks_valid(suffix, data):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{f.filename}: not a PDF or UTF-8 markdown file")
        payloads.append((_safe_stem(f.filename), suffix, data))
    stems = [stem for stem, _, _ in payloads]
    if len(set(stems)) != len(stems):   # "a.pdf" and "A.pdf" would overwrite each other
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"file names collide as doc_ids: {stems}")

    dest = UPLOAD_DIR / tenant_id
    dest.mkdir(parents=True, exist_ok=True)
    saved = []
    for (stem, suffix, data), meta in zip(payloads, metas):
        path = dest / f"{stem}{suffix}"
        path.write_bytes(data)   # same name again = re-ingest; unchanged chunks are skipped by hash
        saved.append((path, meta))
    return _start_job(tenant_id, saved)


@router.get("/{job_id}", response_model=IngestionJob, summary="Ingestion progress")
def get_ingestion_job(tenant_id: TenantId, job_id: str) -> IngestionJob:
    return jobs.ingestion_jobs.get(tenant_id, job_id)
