from fastapi import APIRouter

from app.api.schemas import TenantId
from app.documents import IndexedDocument, indexed_documents
from app.store import connect

router = APIRouter(prefix="/tenants/{tenant_id}/documents", tags=["documents"])


@router.get("", response_model=list[IndexedDocument],
            summary="Filings in this tenant's index, with chunk/page counts")
def list_documents(tenant_id: TenantId) -> list[IndexedDocument]:
    with connect() as conn:
        return indexed_documents(conn, tenant_id)
