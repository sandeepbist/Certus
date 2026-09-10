from decimal import Decimal

from fastapi import APIRouter, Depends

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity

router = APIRouter(prefix="", tags=["Workspace Dashboard"])


def _json_number(value, default=0):
    if value is None:
        return default
    return float(value) if isinstance(value, Decimal) else value


@router.get("/dashboard/overview")
def dashboard_overview(identity: RequestIdentity = Depends(require_request_identity)):
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL) AS documents,
                   COUNT(*) FILTER (
                       WHERE deleted_at IS NULL
                         AND created_at >= (
                             date_trunc('day', NOW() AT TIME ZONE 'UTC')
                                 AT TIME ZONE 'UTC'
                         ) - INTERVAL '6 days'
                   ) AS documents_this_week
            FROM documents
            WHERE tenant_id = %s AND user_id = %s
            """,
            (identity.tenant_id, identity.user_id),
        )
        documents = dict(cursor.fetchone())

        cursor.execute(
            """
            SELECT COUNT(*) AS queries,
                   AVG(eval_score) FILTER (
                       WHERE grounding_profile = 'certus_atomic_claim_evidence:v1'
                         AND eval_details ->> 'metric' = 'mechanical_claim_evidence_integrity'
                   ) AS avg_claim_evidence_integrity,
                   COALESCE(SUM(total_tokens) FILTER (
                       WHERE created_at >= (
                           date_trunc('day', NOW() AT TIME ZONE 'UTC')
                               AT TIME ZONE 'UTC'
                       )
                   ), 0) AS tokens_today,
                   COALESCE(SUM(estimated_cost_usd) FILTER (
                       WHERE created_at >= (
                           date_trunc('day', NOW() AT TIME ZONE 'UTC')
                               AT TIME ZONE 'UTC'
                       )
                   ), 0.0) AS cost_today
            FROM agent_runs
            WHERE tenant_id = %s AND user_id = %s
            """,
            (identity.tenant_id, identity.user_id),
        )
        runs = dict(cursor.fetchone())

        cursor.execute(
            """
            SELECT COUNT(*) FILTER (
                       WHERE status IN ('pending', 'in_progress')
                   ) AS active_tasks,
                   COUNT(*) FILTER (
                       WHERE status IN ('pending', 'in_progress')
                         AND due_date >= CURRENT_DATE
                         AND due_date < CURRENT_DATE + INTERVAL '1 day'
                   ) AS due_today
            FROM tasks
            WHERE tenant_id = %s AND user_id = %s
            """,
            (identity.tenant_id, identity.user_id),
        )
        tasks = dict(cursor.fetchone())

        cursor.execute(
            """
            SELECT COALESCE(max_token_budget_daily, 100000) AS token_budget
            FROM tenant_config
            WHERE organization_id = %s
            """,
            (identity.tenant_id,),
        )
        config = cursor.fetchone()

        cursor.execute(
            """
            SELECT activity_type, activity_id, title, detail, status, created_at, href
            FROM (
                SELECT 'document' AS activity_type, id::text AS activity_id,
                       title,
                       source_type || ' · ' || COALESCE(chunk_count, 0)::text || ' chunks' AS detail,
                       status, created_at,
                       '/documents/' || id::text AS href
                FROM documents
                WHERE tenant_id = %s AND user_id = %s AND deleted_at IS NULL

                UNION ALL

                SELECT 'query', id::text,
                       LEFT(input_query, 140),
                       COALESCE(model_used, 'model pending'),
                       status, created_at,
                       '/traces/' || id::text
                FROM agent_runs
                WHERE tenant_id = %s AND user_id = %s

                UNION ALL

                SELECT 'task', id::text, title,
                       priority || ' priority',
                       status, created_at,
                       '/tasks'
                FROM tasks
                WHERE tenant_id = %s AND user_id = %s

                UNION ALL

                SELECT 'automation', execution.id::text,
                       rule.name,
                       execution.trigger_event,
                       execution.status, execution.started_at,
                       '/automations'
                FROM automation_executions AS execution
                JOIN automation_rules AS rule ON rule.id = execution.rule_id
                WHERE execution.tenant_id = %s AND execution.user_id = %s

                UNION ALL

                SELECT 'memory', id::text, LEFT(fact, 140),
                       COALESCE(category, 'uncategorized'),
                       CASE WHEN is_active THEN 'active' ELSE 'superseded' END,
                       created_at, '/memory'
                FROM memories
                WHERE tenant_id = %s AND user_id = %s
            ) AS activity
            ORDER BY created_at DESC
            LIMIT 10
            """,
            (
                identity.tenant_id,
                identity.user_id,
                identity.tenant_id,
                identity.user_id,
                identity.tenant_id,
                identity.user_id,
                identity.tenant_id,
                identity.user_id,
                identity.tenant_id,
                identity.user_id,
            ),
        )
        activity = [dict(row) for row in cursor.fetchall()]

    token_budget = int(_json_number(config["token_budget"] if config else None, 100000))
    tokens_today = int(_json_number(runs.get("tokens_today"), 0))
    return {
        "metrics": {
            "documents": int(_json_number(documents.get("documents"), 0)),
            "documents_this_week": int(_json_number(documents.get("documents_this_week"), 0)),
            "queries": int(_json_number(runs.get("queries"), 0)),
            "avg_claim_evidence_integrity": (
                float(_json_number(runs.get("avg_claim_evidence_integrity")))
                if runs.get("avg_claim_evidence_integrity") is not None
                else None
            ),
            "active_tasks": int(_json_number(tasks.get("active_tasks"), 0)),
            "due_today": int(_json_number(tasks.get("due_today"), 0)),
            "tokens_today": tokens_today,
            "token_budget": token_budget,
            "cost_today_usd": float(_json_number(runs.get("cost_today"), 0.0)),
        },
        "recent_activity": activity,
    }
