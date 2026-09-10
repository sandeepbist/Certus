import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity
from app.retrieval.notifications_view import notification_record


router = APIRouter(prefix="", tags=["Notifications"])
NotificationStatus = Literal["all", "unread", "read"]
NotificationBulkOperation = Literal["read", "unread", "delete"]

NOTIFICATION_FIELDS = """
    id, type, title, body, metadata, action_url, is_read, created_at
"""


class NotificationReadUpdate(BaseModel):
    is_read: bool


class NotificationBulkAction(BaseModel):
    operation: NotificationBulkOperation
    ids: list[uuid.UUID] = Field(min_length=1, max_length=100)

    @field_validator("ids")
    @classmethod
    def unique_ids(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        return list(dict.fromkeys(value))


@router.get("/notifications")
def list_notifications(
    status: NotificationStatus = "all",
    notification_type: str | None = Query(default=None, alias="type", min_length=1, max_length=50),
    limit: int = Query(default=25, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    filters = ["organization_id = %s", "user_id = %s"]
    params: list[object] = [identity.tenant_id, identity.user_id]
    if status != "all":
        filters.append("is_read = %s")
        params.append(status == "read")
    if notification_type:
        filters.append("type = %s")
        params.append(notification_type)

    where_clause = " AND ".join(filters)
    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                f"""
                SELECT created_at, id
                FROM notifications
                WHERE id = %s AND {where_clause}
                """,
                [str(page_cursor), *params],
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(
                    status_code=422,
                    detail="The notification page cursor is invalid.",
                )
        else:
            anchor = None

        cursor.execute(
            """
            SELECT COUNT(*) AS unread_count
            FROM notifications
            WHERE organization_id = %s AND user_id = %s AND is_read = false
            """,
            (identity.tenant_id, identity.user_id),
        )
        unread_count = int(cursor.fetchone()["unread_count"])

        cursor.execute(
            """
            SELECT DISTINCT type
            FROM notifications
            WHERE organization_id = %s AND user_id = %s
            ORDER BY type ASC
            LIMIT 100
            """,
            (identity.tenant_id, identity.user_id),
        )
        available_types = [row["type"] for row in cursor.fetchall()]

        page_filter = ""
        page_params = list(params)
        if anchor:
            page_filter = "AND (created_at, id) < (%s, %s)"
            page_params.extend([anchor["created_at"], anchor["id"]])
        page_params.append(limit + 1)
        cursor.execute(
            f"""
            SELECT {NOTIFICATION_FIELDS}
            FROM notifications
            WHERE {where_clause}
              {page_filter}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """,
            page_params,
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        notifications = [notification_record(dict(row)) for row in rows[:limit]]

    return {
        "notifications": notifications,
        "unread_count": unread_count,
        "available_types": available_types,
        "pagination": {
            "limit": limit,
            "next_cursor": str(notifications[-1]["id"]) if has_more else None,
        },
    }


@router.patch("/notifications/{notification_id}/read")
def update_notification_read_state(
    notification_id: uuid.UUID,
    request: NotificationReadUpdate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE notifications
            SET is_read = %s
            WHERE id = %s AND organization_id = %s AND user_id = %s
            RETURNING {NOTIFICATION_FIELDS}
            """,
            (request.is_read, str(notification_id), identity.tenant_id, identity.user_id),
        )
        notification = cursor.fetchone()
        if not notification:
            raise HTTPException(status_code=404, detail="Notification not found")
    return {"notification": notification_record(dict(notification))}


@router.post("/notifications/read-all")
def mark_all_notifications_read(
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            WITH updated AS (
                UPDATE notifications
                SET is_read = true
                WHERE organization_id = %s AND user_id = %s AND is_read = false
                RETURNING 1
            )
            SELECT COUNT(*) FROM updated
            """,
            (identity.tenant_id, identity.user_id),
        )
        updated_count = int(cursor.fetchone()[0])
    return {"updated_count": updated_count, "unread_count": 0}


@router.post("/notifications/bulk")
def bulk_update_notifications(
    request: NotificationBulkAction,
    identity: RequestIdentity = Depends(require_request_identity),
):
    notification_ids = [str(notification_id) for notification_id in request.ids]
    with get_db_cursor(dict_cursor=False) as cursor:
        if request.operation == "delete":
            cursor.execute(
                """
                DELETE FROM notifications
                WHERE id = ANY(%s::uuid[]) AND organization_id = %s AND user_id = %s
                RETURNING id
                """,
                (notification_ids, identity.tenant_id, identity.user_id),
            )
        else:
            cursor.execute(
                """
                UPDATE notifications
                SET is_read = %s
                WHERE id = ANY(%s::uuid[]) AND organization_id = %s AND user_id = %s
                RETURNING id
                """,
                (
                    request.operation == "read",
                    notification_ids,
                    identity.tenant_id,
                    identity.user_id,
                ),
            )
        affected_ids = [str(row[0]) for row in cursor.fetchall()]
    return {"operation": request.operation, "affected_ids": affected_ids, "affected_count": len(affected_ids)}


@router.delete("/notifications/{notification_id}")
def delete_notification(
    notification_id: uuid.UUID,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            DELETE FROM notifications
            WHERE id = %s AND organization_id = %s AND user_id = %s
            RETURNING id
            """,
            (str(notification_id), identity.tenant_id, identity.user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Notification not found")
    return {"deleted": True, "notification_id": str(notification_id)}
