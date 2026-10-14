from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..core.identity import RequestIdentity, require_request_identity
from ..retrieval.graphrag import document_graph
from ..retrieval.graph_view import project_workspace_graph


router = APIRouter(prefix="/graph", tags=["Knowledge Graph"])


@router.get("")
def get_workspace_graph(
    identity: RequestIdentity = Depends(require_request_identity),
    query: str = Query("", max_length=500),
    depth: int = Query(2, ge=1, le=4),
    limit: int = Query(200, ge=1, le=250),
    entity_type: Optional[str] = Query(None, max_length=50),
):
    try:
        projection = document_graph.workspace(
            identity.tenant_id, identity.user_id, entity_type.upper() if entity_type else None,
        )
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="The knowledge graph is temporarily unavailable.",
        ) from error

    result = project_workspace_graph(projection.rows[:2000], query=query, depth=depth, node_limit=limit)
    result["truncated"] = bool(result["truncated"] or projection.truncated)
    result["projection_status"] = projection.status
    result["tenant_scope"] = "authenticated_workspace"
    return result
