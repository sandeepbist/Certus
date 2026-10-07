"""Original-file export uses the ingestion service's scoped, verified reads."""

import uuid
from time import monotonic

import httpx
from fastapi import HTTPException

from app.core.config import settings
from app.core.identity import RequestIdentity
from app.retrieval.export_archive import ExportArchiveLimitExceeded, ExportOriginal


def plan_export_originals(data: dict, identity: RequestIdentity, requested: bool) -> dict:
    sources = {str(row["document_version_id"]): row for row in data["document_source_objects"]}
    files = []
    total_bytes = 0
    included_count = 0
    for version in data["document_versions"]:
        source = sources.get(str(version["id"]))
        if requested and (
            source is None or source["tenant_id"] != identity.tenant_id
            or source["user_id"] != identity.user_id
            or str(source["document_id"]) != str(version["document_id"])
        ):
            raise HTTPException(status_code=409, detail="An original catalog entry is unavailable or inconsistent.")
        entry = {
            "document_id": str(version["document_id"]),
            "document_version_id": str(version["id"]),
            "version_number": version["version_number"],
            "source_object_id": str(source["id"]) if source else None,
            "original_filename": source["original_filename"] if source else None,
            "byte_length": source["byte_length"] if source else None,
            "sha256": source["content_sha256"] if source else None,
            "path": None,
            "status": "excluded",
            "reason": "not_requested",
        }
        if requested:
            if source["status"] in {"unavailable", "delete_pending", "deleted"}:
                entry["reason"] = source["status"]
            elif source["status"] != "available" or source["storage_backend"] != "s3":
                raise HTTPException(status_code=409, detail="An original is temporarily unavailable. Retry after storage recovery.")
            else:
                # UUID paths never use user-supplied filenames as ZIP entry names.
                entry["path"] = (
                    f"originals/{uuid.UUID(entry['document_id'])}/"
                    f"{uuid.UUID(entry['document_version_id'])}/{uuid.UUID(entry['source_object_id'])}.bin"
                )
                entry["status"] = "included"
                entry["reason"] = None
                total_bytes += int(source["byte_length"])
                included_count += 1
        files.append(entry)
    if total_bytes > settings.MAX_EXPORT_ORIGINAL_BYTES or included_count > settings.MAX_EXPORT_ORIGINAL_FILES:
        raise ExportArchiveLimitExceeded("Export originals exceed the synchronous byte or file limit")
    return {
        "requested": requested,
        "scope": "all_owned_versions_including_archived_documents",
        "unavailable_policy": "explicit_exclusions; unexpected_download_failures_abort",
        "included_count": included_count,
        "excluded_count": len(files) - included_count,
        "complete": included_count == len(files),
        "total_bytes": total_bytes,
        "files": files,
    }


def download_export_originals(manifest: dict, identity: RequestIdentity, deadline: float):
    included = [entry for entry in manifest["files"] if entry["status"] == "included"]
    if not included:
        return
    if not settings.INTERNAL_SERVICE_TOKEN:
        raise HTTPException(status_code=503, detail="Original export authentication is not configured.")
    headers = {
        "X-Internal-Service-Token": settings.INTERNAL_SERVICE_TOKEN,
        "X-Certus-Tenant-Id": identity.tenant_id,
        "X-Certus-User-Id": identity.user_id,
        "Accept-Encoding": "identity",
    }
    # One scoped client per infrequent export; reuse connections for every file,
    # then close deterministically even when ZIP construction fails.
    with httpx.Client(headers=headers, follow_redirects=False, trust_env=False) as client:
        for entry in included:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise HTTPException(status_code=504, detail="The data export exceeded its time limit.")
            with client.stream(
                "GET",
                f"{settings.INGESTION_SERVICE_URL.rstrip('/')}/documents/{entry['document_id']}/original",
                params={"version": entry["version_number"], "include_archived": "true"},
                timeout=httpx.Timeout(
                    remaining, connect=min(3, remaining), pool=min(2, remaining), read=min(5, remaining),
                ),
            ) as response:
                if response.status_code != 200:
                    raise HTTPException(status_code=503, detail="An original could not be verified. No export was saved.")
                yield ExportOriginal(
                    path=entry["path"],
                    byte_length=int(entry["byte_length"]),
                    sha256=entry["sha256"],
                    chunks=response.iter_bytes(),
                )
