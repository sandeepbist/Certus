from dataclasses import dataclass

from fastapi import Header, HTTPException


@dataclass(frozen=True)
class RequestIdentity:
    tenant_id: str
    user_id: str


def require_request_identity(
    tenant_id: str = Header(..., alias="X-Certus-Tenant-Id"),
    user_id: str = Header(..., alias="X-Certus-User-Id"),
) -> RequestIdentity:
    tenant_id = tenant_id.strip()
    user_id = user_id.strip()
    if not tenant_id or not user_id:
        raise HTTPException(status_code=401, detail="A trusted request identity is required")
    return RequestIdentity(tenant_id=tenant_id, user_id=user_id)
