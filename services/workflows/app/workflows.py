from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError


ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=16),
    maximum_attempts=5,
)

WEBHOOK_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=16),
    maximum_attempts=3,
)


@workflow.defn(name="certus.reminder.v1")
class ReminderWorkflow:
    def __init__(self) -> None:
        self._status = "starting"

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        scheduled_for = datetime.fromisoformat(payload["scheduled_for"])
        if scheduled_for.tzinfo is None:
            scheduled_for = scheduled_for.replace(tzinfo=timezone.utc)

        self._status = "waiting"
        delay = scheduled_for - workflow.now()
        if delay.total_seconds() > 0:
            await workflow.sleep(delay, summary="Wait until reminder delivery time")

        self._status = "delivering"
        try:
            result = await workflow.execute_activity(
                "deliver_reminder",
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(hours=1),
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
        except ActivityError as error:
            self._status = "failed"
            await workflow.execute_activity(
                "record_reminder_failure",
                {**payload, "error_type": type(error).__name__},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            raise
        self._status = "delivered"
        return result


@workflow.defn(name="certus.automation.v1")
class AutomationWorkflow:
    def __init__(self) -> None:
        self._status = "scheduled"

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._status = "running"
        try:
            result = await workflow.execute_activity(
                "execute_automation",
                payload,
                start_to_close_timeout=timedelta(minutes=2),
                schedule_to_close_timeout=timedelta(hours=1),
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
        except ActivityError as error:
            self._status = "failed"
            await workflow.execute_activity(
                "record_automation_failure",
                {**payload, "error_type": type(error).__name__},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            raise
        self._status = result.get("status", "completed")
        return result


@workflow.defn(name="certus.webhook-event.v1")
class WebhookEventWorkflow:
    def __init__(self) -> None:
        self._status = "scheduled"

    @workflow.query
    def status(self) -> str:
        return self._status

    @workflow.run
    async def run(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._status = "loading_targets"
        try:
            target_result = await workflow.execute_activity(
                "list_webhook_targets",
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(hours=1),
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
        except ActivityError as error:
            self._status = "failed"
            await workflow.execute_activity(
                "record_webhook_event_result",
                {
                    **payload,
                    "target_count": 0,
                    "delivered": 0,
                    "skipped": 0,
                    "failed": 1,
                    "last_error": str(error)[:500],
                },
                start_to_close_timeout=timedelta(seconds=30),
                schedule_to_close_timeout=timedelta(hours=1),
                retry_policy=ACTIVITY_RETRY_POLICY,
            )
            raise

        webhook_ids = list(target_result.get("webhook_ids", []))
        delivered = 0
        skipped = 0
        failed = 0
        last_error: str | None = None
        self._status = "delivering"
        for webhook_id in webhook_ids:
            try:
                result = await workflow.execute_activity(
                    "deliver_webhook_event",
                    {**payload, "webhook_id": webhook_id},
                    start_to_close_timeout=timedelta(seconds=30),
                    schedule_to_close_timeout=timedelta(hours=1),
                    retry_policy=WEBHOOK_RETRY_POLICY,
                )
                if result.get("status") == "skipped":
                    skipped += 1
                else:
                    delivered += 1
            except ActivityError as error:
                failed += 1
                last_error = str(error)[:500]

        result = await workflow.execute_activity(
            "record_webhook_event_result",
            {
                **payload,
                "target_count": len(webhook_ids),
                "delivered": delivered,
                "skipped": skipped,
                "failed": failed,
                "last_error": last_error,
            },
            start_to_close_timeout=timedelta(seconds=30),
            schedule_to_close_timeout=timedelta(hours=1),
            retry_policy=ACTIVITY_RETRY_POLICY,
        )
        self._status = result["status"]
        return result
