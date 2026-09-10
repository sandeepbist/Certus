import { getDb } from '@/lib/db';

export async function remainingDailyTokenBudget(tenantId: string, userId: string) {
  const result = await getDb().query<{ remaining_tokens: number }>(
    `WITH utc_day AS (
       SELECT date_trunc('day', NOW() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' AS starts_at
     ),
     configured_budget AS (
       SELECT COALESCE(
         (SELECT max_token_budget_daily
          FROM tenant_config
          WHERE organization_id = $1),
         100000
       )::bigint AS allocated_tokens
     ),
     usage AS (
       SELECT COALESCE(SUM(
         CASE
           WHEN status = 'running' THEN GREATEST(total_tokens, reserved_tokens)
           ELSE total_tokens
         END
       ), 0)::bigint AS consumed_or_reserved_tokens
       FROM agent_runs, utc_day
       WHERE tenant_id = $1
         AND user_id = $2
         AND created_at >= utc_day.starts_at
         AND created_at < utc_day.starts_at + INTERVAL '1 day'
     )
     SELECT GREATEST(
       configured_budget.allocated_tokens - usage.consumed_or_reserved_tokens,
       0
     )::int AS remaining_tokens
     FROM configured_budget, usage`,
    [tenantId, userId],
  );

  return result.rows[0]?.remaining_tokens ?? 0;
}
