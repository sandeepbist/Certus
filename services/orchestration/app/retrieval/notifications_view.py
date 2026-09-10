from typing import Any


def safe_action_url(value: Any) -> str | None:
    """Return only same-origin application paths from stored notification data."""
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path.startswith("/") or path.startswith("//") or len(path) > 500:
        return None
    return path


def notification_record(row: dict[str, Any]) -> dict[str, Any]:
    record = dict(row)
    record["action_url"] = safe_action_url(record.get("action_url"))
    record["metadata"] = record.get("metadata") or {}
    return record

