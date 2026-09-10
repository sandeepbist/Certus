import uuid
import json
import re
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from typing import Literal, Optional, Dict, Any
from app.core.db import get_db_cursor
from app.core.identity import RequestIdentity, require_request_identity

router = APIRouter(prefix="", tags=["Automations & Rules Engine"])

class AutomationRuleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    trigger_type: Literal["on_document_uploaded"] = "on_document_uploaded"
    condition_expression: str = Field(default="true", max_length=500)
    action_type: Literal["summarize_and_create_task", "notify"] = "summarize_and_create_task"
    action_config: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value: str) -> str:
        clean_value = value.strip()
        if not clean_value:
            raise ValueError("Automation name cannot be blank")
        return clean_value


class AutomationRuleUpdate(BaseModel):
    version: int = Field(ge=1)
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    is_active: Optional[bool] = None


def parse_condition_expression(expression: str) -> Dict[str, Any]:
    clean_expression = expression.strip()
    if clean_expression.casefold() == "true":
        return {"type": "always"}

    tag_match = re.fullmatch(
        r"tags\.includes\(\s*['\"]([^'\"]{1,64})['\"]\s*\)",
        clean_expression,
    )
    if tag_match:
        return {"type": "tag_contains", "value": tag_match.group(1).casefold()}

    mime_match = re.fullmatch(
        r"mime_type\s*==={0,1}\s*['\"]([^'\"]{1,100})['\"]",
        clean_expression,
    )
    if mime_match:
        return {"type": "mime_type_equals", "value": mime_match.group(1)}

    raise HTTPException(
        status_code=422,
        detail=(
            "Unsupported condition. Use true, tags.includes('tag'), or "
            "mime_type == 'application/pdf'."
        ),
    )

@router.get("/automations")
def list_automations(
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                """
                SELECT created_at, id
                FROM automation_rules
                WHERE id = %s AND tenant_id = %s AND user_id = %s
                """,
                (str(page_cursor), identity.tenant_id, identity.user_id),
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(status_code=422, detail="The automation page cursor is invalid.")
        else:
            anchor = None

        params: list[object] = [identity.tenant_id, identity.user_id]
        page_filter = ""
        if anchor:
            page_filter = "AND (created_at, id) < (%s, %s)"
            params.extend([anchor["created_at"], anchor["id"]])
        params.append(limit + 1)
        cursor.execute(
            """
            SELECT id, name, trigger_event AS trigger_type, trigger_conditions,
                   actions, is_enabled AS is_active, execution_count, last_executed_at,
                   version, created_at, updated_at
            FROM automation_rules
            WHERE tenant_id = %s AND user_id = %s
              {page_filter}
            ORDER BY created_at DESC, id DESC
            LIMIT %s
            """.format(page_filter=page_filter),
            params,
        )
        rules = cursor.fetchall()
        has_more = len(rules) > limit
        formatted = []
        for r in rules[:limit]:
            rd = dict(r)
            cond = rd.get("trigger_conditions") or {}
            condition_type = cond.get("type", "always")
            if condition_type == "tag_contains":
                rd["condition_expression"] = f"tags.includes('{cond.get('value', '')}')"
            elif condition_type == "mime_type_equals":
                rd["condition_expression"] = f"mime_type == '{cond.get('value', '')}'"
            else:
                rd["condition_expression"] = "true"
            acts = rd.get("actions") or []
            rd["action_type"] = acts[0].get("type", "default_action") if acts else "default_action"
            formatted.append(rd)
        return {
            "rules": formatted,
            "pagination": {
                "limit": limit,
                "next_cursor": str(formatted[-1]["id"]) if has_more else None,
            },
        }

@router.post("/automations")
def create_automation(
    rule: AutomationRuleCreate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    rule_id = str(uuid.uuid4())
    cond_json = json.dumps(parse_condition_expression(rule.condition_expression))
    acts_json = json.dumps([{"type": rule.action_type, "config": rule.action_config}])
    
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            INSERT INTO automation_rules (
                id, tenant_id, user_id, name, trigger_event,
                trigger_conditions, actions, is_enabled, execution_count, version, created_at, updated_at
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, true, 0, 1, NOW(), NOW()
            )
            """,
            (
                rule_id, identity.tenant_id, identity.user_id,
                rule.name, rule.trigger_type, cond_json, acts_json
            )
        )
    return {"rule_id": rule_id, "name": rule.name, "status": "active", "version": 1}


@router.patch("/automations/{rule_id}")
def update_automation(
    rule_id: uuid.UUID,
    request: AutomationRuleUpdate,
    identity: RequestIdentity = Depends(require_request_identity),
):
    updates = request.model_dump(exclude={"version"}, exclude_unset=True)
    if not updates:
        raise HTTPException(status_code=422, detail="At least one automation field must be updated")
    if "is_active" in updates:
        updates["is_enabled"] = updates.pop("is_active")
    assignments = [f"{field} = %s" for field in updates]
    params = list(updates.values())
    params.extend([str(rule_id), identity.tenant_id, identity.user_id, request.version])

    with get_db_cursor() as cursor:
        cursor.execute(
            f"""
            UPDATE automation_rules
            SET {', '.join(assignments)}, version = version + 1, updated_at = NOW()
            WHERE id = %s AND tenant_id = %s AND user_id = %s AND version = %s
            RETURNING id, name, trigger_event AS trigger_type, trigger_conditions,
                      actions, is_enabled AS is_active, execution_count, last_executed_at,
                      version, created_at, updated_at
            """,
            params,
        )
        rule = cursor.fetchone()
        if not rule:
            cursor.execute(
                "SELECT version FROM automation_rules WHERE id = %s AND tenant_id = %s AND user_id = %s",
                (str(rule_id), identity.tenant_id, identity.user_id),
            )
            current = cursor.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Automation not found")
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "version_conflict",
                    "message": "The automation changed since it was loaded.",
                    "current_version": current["version"],
                },
            )
        return {"rule": dict(rule)}


@router.get("/automations/{rule_id}/history")
def automation_history(
    rule_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=100),
    page_cursor: uuid.UUID | None = Query(default=None, alias="cursor"),
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor() as cursor:
        if page_cursor is not None:
            cursor.execute(
                """
                SELECT started_at, id
                FROM automation_executions
                WHERE id = %s AND rule_id = %s AND tenant_id = %s AND user_id = %s
                """,
                (str(page_cursor), str(rule_id), identity.tenant_id, identity.user_id),
            )
            anchor = cursor.fetchone()
            if not anchor:
                raise HTTPException(
                    status_code=422,
                    detail="The automation history cursor is invalid.",
                )
        else:
            anchor = None

        params: list[object] = [str(rule_id), identity.tenant_id, identity.user_id]
        page_filter = ""
        if anchor:
            page_filter = "AND (started_at, id) < (%s, %s)"
            params.extend([anchor["started_at"], anchor["id"]])
        params.append(limit + 1)
        cursor.execute(
            """
            SELECT id, trigger_event, trigger_event_id, workflow_id, source_document_id,
                   created_task_id, status, result, error_message, started_at, completed_at
            FROM automation_executions
            WHERE rule_id = %s AND tenant_id = %s AND user_id = %s
              {page_filter}
            ORDER BY started_at DESC, id DESC
            LIMIT %s
            """.format(page_filter=page_filter),
            params,
        )
        rows = cursor.fetchall()
        has_more = len(rows) > limit
        executions = [dict(execution) for execution in rows[:limit]]
        return {
            "executions": executions,
            "pagination": {
                "limit": limit,
                "next_cursor": str(executions[-1]["id"]) if has_more else None,
            },
        }

@router.delete("/automations/{rule_id}")
def delete_automation(
    rule_id: str,
    identity: RequestIdentity = Depends(require_request_identity),
):
    with get_db_cursor(dict_cursor=False) as cursor:
        cursor.execute(
            """
            DELETE FROM automation_rules
            WHERE id = %s AND tenant_id = %s AND user_id = %s
            RETURNING id
            """,
            (rule_id, identity.tenant_id, identity.user_id),
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Automation not found")
    return {"deleted": True, "rule_id": rule_id}
