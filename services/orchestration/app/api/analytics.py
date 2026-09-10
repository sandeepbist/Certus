from fastapi import APIRouter, Depends, Query

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.analytics_view import fill_daily_series, number

router = APIRouter(prefix="", tags=["Analytics & Usage Control"])


@router.get("/analytics/usage")
def get_usage_analytics(
    days: int = Query(default=30, ge=7, le=90),
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS total_queries,
                   COALESCE(SUM(total_tokens), 0) AS total_tokens,
                   COALESCE(SUM(estimated_cost_usd), 0.0) AS total_cost_usd,
                   AVG(latency_ms) FILTER (WHERE latency_ms IS NOT NULL) AS avg_latency_ms,
                   AVG(eval_score) FILTER (
                       WHERE grounding_profile = 'certus_atomic_claim_evidence:v1'
                         AND eval_details ->> 'metric' = 'mechanical_claim_evidence_integrity'
                   ) AS avg_claim_evidence_integrity,
                   percentile_cont(0.50) WITHIN GROUP (ORDER BY latency_ms)
                       FILTER (WHERE latency_ms IS NOT NULL) AS p50_ms,
                   percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms)
                       FILTER (WHERE latency_ms IS NOT NULL) AS p95_ms,
                   percentile_cont(0.99) WITHIN GROUP (ORDER BY latency_ms)
                       FILTER (WHERE latency_ms IS NOT NULL) AS p99_ms
            FROM agent_runs
            WHERE tenant_id = %s
              AND user_id = %s
              AND created_at >= (
                  date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
              ) - (%s - 1) * INTERVAL '1 day'
            """,
            (identity.tenant_id, identity.user_id, days),
        )
        overview = dict(cursor.fetchone())

        cursor.execute(
            """
            SELECT COALESCE(model_used, 'unknown') AS model_used,
                   COUNT(*) AS count,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd
            FROM agent_runs
            WHERE tenant_id = %s
              AND user_id = %s
              AND created_at >= (
                  date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
              ) - (%s - 1) * INTERVAL '1 day'
            GROUP BY COALESCE(model_used, 'unknown')
            ORDER BY count DESC, model_used ASC
            """,
            (identity.tenant_id, identity.user_id, days),
        )
        models = [dict(model) for model in cursor.fetchall()]

        cursor.execute(
            """
            SELECT (created_at AT TIME ZONE 'UTC')::date AS day,
                   COUNT(*) AS queries,
                   COALESCE(SUM(total_tokens), 0) AS tokens,
                   COALESCE(SUM(estimated_cost_usd), 0.0) AS cost_usd
            FROM agent_runs
            WHERE tenant_id = %s
              AND user_id = %s
              AND created_at >= (
                  date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC'
              ) - (%s - 1) * INTERVAL '1 day'
            GROUP BY day
            ORDER BY day ASC
            """,
            (identity.tenant_id, identity.user_id, days),
        )
        daily_rows = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            """
            WITH utc_day AS (
                SELECT date_trunc('day', NOW() AT TIME ZONE 'UTC')
                           AT TIME ZONE 'UTC' AS starts_at
            )
            SELECT COUNT(*) FILTER (
                       WHERE created_at >= utc_day.starts_at
                   ) AS current_queries,
                   COUNT(*) FILTER (
                       WHERE created_at >= utc_day.starts_at - INTERVAL '1 day'
                         AND created_at < utc_day.starts_at
                   ) AS previous_queries,
                   COALESCE(SUM(total_tokens) FILTER (
                       WHERE created_at >= utc_day.starts_at
                   ), 0) AS tokens_today,
                   COALESCE(SUM(
                       CASE
                           WHEN status = 'running'
                               THEN GREATEST(total_tokens, reserved_tokens) - total_tokens
                           ELSE 0
                       END
                   ) FILTER (
                       WHERE created_at >= utc_day.starts_at
                   ), 0) AS tokens_reserved
            FROM agent_runs, utc_day
            WHERE tenant_id = %s AND user_id = %s
              AND created_at >= utc_day.starts_at - INTERVAL '1 day'
            """,
            (identity.tenant_id, identity.user_id),
        )
        today = dict(cursor.fetchone())

        cursor.execute(
            """
            SELECT COALESCE(max_token_budget_daily, 100000) AS allocated_tokens
            FROM tenant_config
            WHERE organization_id = %s
            """,
            (identity.tenant_id,),
        )
        tenant_config = cursor.fetchone()

        cursor.execute(
            """
            SELECT COUNT(*) FILTER (WHERE deleted_at IS NULL) AS total_documents,
                   COUNT(*) FILTER (
                       WHERE deleted_at IS NULL AND status = 'ready'
                   ) AS ready_documents,
                   COALESCE(SUM(file_size_bytes) FILTER (
                       WHERE deleted_at IS NULL
                   ), 0) AS storage_bytes,
                   COALESCE(SUM(chunk_count) FILTER (
                       WHERE deleted_at IS NULL
                   ), 0) AS chunks
            FROM documents
            WHERE tenant_id = %s AND user_id = %s
            """,
            (identity.tenant_id, identity.user_id),
        )
        document_stats = dict(cursor.fetchone())

    current_queries = int(number(today.get("current_queries"), 0))
    previous_queries = int(number(today.get("previous_queries"), 0))
    query_change_percent = None
    if previous_queries > 0:
        query_change_percent = round(((current_queries - previous_queries) / previous_queries) * 100, 1)

    allocated_tokens = int(
        number(tenant_config.get("allocated_tokens") if tenant_config else None, 100000)
    )
    used_tokens = int(number(today.get("tokens_today"), 0))
    reserved_tokens = int(number(today.get("tokens_reserved"), 0))

    return {
        "period_days": days,
        "overview": {
            "total_queries": int(number(overview.get("total_queries"), 0)),
            "total_tokens": int(number(overview.get("total_tokens"), 0)),
            "total_cost_usd": float(number(overview.get("total_cost_usd"), 0.0)),
            "avg_latency_ms": (
                round(float(number(overview.get("avg_latency_ms"))))
                if overview.get("avg_latency_ms") is not None
                else None
            ),
            "avg_claim_evidence_integrity": (
                float(number(overview.get("avg_claim_evidence_integrity")))
                if overview.get("avg_claim_evidence_integrity") is not None
                else None
            ),
            "queries_today": current_queries,
            "queries_yesterday": previous_queries,
            "query_change_percent": query_change_percent,
        },
        "model_distribution": [
            {
                "model_used": model["model_used"],
                "count": int(number(model.get("count"), 0)),
                "tokens": int(number(model.get("tokens"), 0)),
                "cost_usd": float(number(model.get("cost_usd"), 0.0)),
            }
            for model in models
        ],
        "latency_percentiles": {
            key: round(float(number(overview.get(key)))) if overview.get(key) is not None else None
            for key in ("p50_ms", "p95_ms", "p99_ms")
        },
        "daily_budget": {
            "allocated_tokens": allocated_tokens,
            "used_tokens": used_tokens,
            "reserved_tokens": reserved_tokens,
            "remaining_tokens": max(0, allocated_tokens - used_tokens - reserved_tokens),
        },
        "daily_series": fill_daily_series(daily_rows, days),
        "document_stats": {
            "total_documents": int(number(document_stats.get("total_documents"), 0)),
            "ready_documents": int(number(document_stats.get("ready_documents"), 0)),
            "storage_bytes": int(number(document_stats.get("storage_bytes"), 0)),
            "chunks": int(number(document_stats.get("chunks"), 0)),
        },
    }
