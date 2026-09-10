from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.graphrag import bounded_graph_query, get_graph_driver
from app.retrieval.graph_view import project_workspace_graph


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
        with get_graph_driver().session() as session:
            records = session.run(
                bounded_graph_query(
                    """
                    MATCH (user:User {id: $user_id})-[:OWNS]->
                          (document:Document {tenant_id: $tenant_id})
                          -[mention:MENTIONS]->(entity:Entity {tenant_id: $tenant_id})
                    WHERE document.deleted_at IS NULL
                      AND ($entity_type IS NULL OR entity.type = $entity_type)
                    RETURN document.id AS document_id,
                           document.title AS document_title,
                           toString(document.created_at) AS document_created_at,
                           entity.normalized_name AS normalized_name,
                           entity.name AS entity_name,
                           entity.type AS entity_type,
                           mention.frequency AS frequency
                    ORDER BY coalesce(mention.frequency, 1) DESC,
                             document.created_at DESC,
                             entity.normalized_name ASC
                    LIMIT 2001
                    """
                ),
                user_id=identity.user_id,
                tenant_id=identity.tenant_id,
                entity_type=entity_type.upper() if entity_type else None,
            )
            rows = [dict(record) for record in records]
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="The knowledge graph is temporarily unavailable.",
        ) from error

    source_truncated = len(rows) > 2000
    result = project_workspace_graph(rows[:2000], query=query, depth=depth, node_limit=limit)
    result["truncated"] = bool(result["truncated"] or source_truncated)
    result["tenant_scope"] = "authenticated_workspace"
    return result
