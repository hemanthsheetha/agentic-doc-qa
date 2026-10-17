import logging
import uuid

from fastapi import APIRouter, status

from app.api import jobs
from app.api.schemas import CreateQueryJobRequest, JobCreated, QueryJob, TenantId

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tenants/{tenant_id}/query-jobs", tags=["queries"])


@router.post("", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreated,
             summary="Ask a question; answered in the background with citations")
def create_query_job(tenant_id: TenantId, request: CreateQueryJobRequest) -> JobCreated:
    job = QueryJob(job_id=uuid.uuid4().hex, tenant_id=tenant_id, status="queued",
                   question=request.question, filters=request.filters, strategy=request.strategy,
                   created_at=jobs.now())
    jobs.query_jobs.add(job)
    jobs.pools["query"].submit(jobs.run_query, job.job_id, tenant_id, request.question, request.filters,
                                request.strategy)
    logger.info("query job %s queued: tenant=%s %r filters=%s",
                job.job_id, tenant_id, request.question[:80], request.filters.model_dump(exclude_none=True))
    return JobCreated(job_id=job.job_id, status=job.status,
                      status_url=f"/tenants/{tenant_id}/query-jobs/{job.job_id}")


@router.get("/{job_id}", response_model=QueryJob, summary="Query status and answer")
def get_query_job(tenant_id: TenantId, job_id: str) -> QueryJob:
    return jobs.query_jobs.get(tenant_id, job_id)
