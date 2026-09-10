from typing import Any, Dict


def condition_matches(condition: Dict[str, Any], event: Dict[str, Any]) -> bool:
    if not condition:
        return True

    condition_type = condition.get("type")
    if not condition_type:
        raise ValueError("Automation condition is missing a supported type")
    if condition_type == "always":
        return True
    if condition_type == "tag_contains":
        expected_tag = str(condition.get("value", "")).casefold()
        return expected_tag in {str(tag).casefold() for tag in event.get("tags", [])}
    if condition_type == "mime_type_equals":
        return event.get("mime_type") == condition.get("value")
    raise ValueError(f"Unsupported automation condition: {condition_type}")


def extract_summary(raw_text: str, limit: int = 900) -> str:
    clean_text = " ".join(raw_text.split())
    if len(clean_text) <= limit:
        return clean_text
    return clean_text[:limit].rsplit(" ", 1)[0] + "…"
