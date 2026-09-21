import re


_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_. -]+")
_LOG_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_log_value(value: object) -> str:
    """Return a bounded, single-line representation for an untrusted log field."""

    # Explicit newline replacement is kept in addition to the full control
    # character pass so the log-forging boundary remains obvious to humans and
    # static analysis. Replace rather than remove separators to avoid joining
    # attacker-controlled fragments into a misleading value.
    text = str(value).replace("\r\n", "_").replace("\n", "_").replace("\r", "_")
    return _LOG_CONTROL_RE.sub("_", text)[:160]


def safe_error_summary(error: BaseException, *, operation: str) -> str:
    """Return bounded diagnostics without copying an exception's message.

    Provider and transport exceptions may retain request headers, credentials,
    URLs, source text, or response bodies. Their string representation is not a
    safe logging or persistence boundary. Exception class names still provide a
    useful failure category without carrying instance data.
    """

    safe_operation = _SAFE_LABEL_RE.sub("_", operation).strip(" ._")[:80]
    if not safe_operation:
        safe_operation = "operation"
    error_type = _SAFE_LABEL_RE.sub("_", type(error).__name__).strip(" ._")[:80]
    if not error_type:
        error_type = "Exception"
    return f"{safe_operation} failed ({error_type})"
