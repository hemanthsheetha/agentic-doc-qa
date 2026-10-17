from fastapi import APIRouter
from pydantic import BaseModel

from app.api.schemas import TenantId
from app.graph.store import graph_stats, rebuild_graph
from app.store import connect

router = APIRouter(prefix="/tenants/{tenant_id}/graph", tags=["graph"])


class GraphStats(BaseModel):
    built: bool
    nodes: dict[str, int]      # by kind: document / section / entity
    entities: dict[str, int]   # by type: service, team, person, role, severity, tier, term
    edges: dict[str, int]      # by kind: references, mentions, defined_in, ...


@router.get("", response_model=GraphStats, summary="What the tenant's context graph contains")
def get_graph(tenant_id: TenantId) -> GraphStats:
    with connect() as conn:
        return GraphStats(**graph_stats(conn, tenant_id))


@router.post("/rebuild", response_model=GraphStats, summary="Rebuild the context graph from the tenant's markdown")
def rebuild(tenant_id: TenantId) -> GraphStats:
    with connect() as conn:
        rebuild_graph(conn, tenant_id)
        return GraphStats(**graph_stats(conn, tenant_id))
