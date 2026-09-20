import re


_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_. -]+")


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
