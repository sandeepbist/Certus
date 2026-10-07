import hashlib
import json
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Callable, Iterable
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.encoders import jsonable_encoder


ARCHIVE_ENTRY_NAME = "certus-export.json"


class ExportArchiveLimitExceeded(ValueError):
    """Raised before an export can grow beyond its in-process byte budget."""


class ExportOriginalIntegrityError(ValueError):
    """An original does not match its immutable catalog entry."""


@dataclass(frozen=True)
class ExportOriginal:
    path: str
    byte_length: int
    sha256: str
    chunks: Iterable[bytes]


class _ArchiveBuffer(BytesIO):
    def __init__(self, limit: int | None):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.limit is not None and self.tell() + len(data) > self.limit:
            raise ExportArchiveLimitExceeded("Export ZIP exceeds the configured archive limit")
        return super().write(data)


class ExportCollectionBudget:
    """Bound row collection before Python can retain an unbounded export."""

    def __init__(self, *, max_source_bytes: int, max_records: int):
        self.max_source_bytes = max_source_bytes
        self.max_records = max_records
        self.source_bytes = 0
        self.records = 0

    def add(self, row: dict) -> None:
        encoded = json.dumps(
            jsonable_encoder(row),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        next_records = self.records + 1
        next_source_bytes = self.source_bytes + len(encoded)
        if next_records > self.max_records:
            raise ExportArchiveLimitExceeded(
                "Export record count exceeds the configured synchronous limit"
            )
        if next_source_bytes > self.max_source_bytes:
            raise ExportArchiveLimitExceeded(
                "Export source data exceeds the configured synchronous byte limit"
            )
        self.records = next_records
        self.source_bytes = next_source_bytes


def build_export_archive(
    payload: dict,
    *,
    max_uncompressed_bytes: int | None = None,
    max_archive_bytes: int | None = None,
    originals: Iterable[ExportOriginal] = (),
    max_original_bytes: int = 0,
    check_deadline: Callable[[], None] = lambda: None,
) -> bytes:
    """Write bounded, digest-verified originals and JSON into one archive."""
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=jsonable_encoder,
    )

    output = _ArchiveBuffer(max_archive_bytes)
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        original_bytes = 0
        names = {ARCHIVE_ENTRY_NAME}
        for original in originals:
            check_deadline()
            path = PurePosixPath(original.path)
            if (
                path.is_absolute() or ".." in path.parts or "\\" in original.path
                or str(path) != original.path or original.path in names
            ):
                raise ExportOriginalIntegrityError("Invalid or duplicate original archive path")
            names.add(original.path)
            if original.byte_length < 0:
                raise ExportOriginalIntegrityError("Invalid cataloged original length")
            if original_bytes + original.byte_length > max_original_bytes:
                raise ExportArchiveLimitExceeded("Export originals exceed the configured byte limit")
            digest = hashlib.sha256()
            written = 0
            with archive.open(original.path, mode="w", force_zip64=True) as entry:
                for chunk in original.chunks:
                    check_deadline()
                    written += len(chunk)
                    if written > original.byte_length:
                        raise ExportOriginalIntegrityError("Original exceeds its cataloged length")
                    digest.update(chunk)
                    entry.write(chunk)
            if written != original.byte_length or digest.hexdigest() != original.sha256:
                raise ExportOriginalIntegrityError("Original failed cataloged SHA-256 verification")
            original_bytes += written
        written = 0
        with archive.open(ARCHIVE_ENTRY_NAME, mode="w", force_zip64=True) as entry:
            for fragment in encoder.iterencode(payload):
                check_deadline()
                encoded = fragment.encode("utf-8")
                written += len(encoded)
                if (
                    max_uncompressed_bytes is not None
                    and written > max_uncompressed_bytes
                ):
                    raise ExportArchiveLimitExceeded(
                        "Export JSON exceeds the configured source byte limit"
                    )
                entry.write(encoded)
    return output.getvalue()


def record_counts(data: dict) -> dict[str, int]:
    return {
        name: (len(value) if isinstance(value, list) else int(value is not None))
        for name, value in data.items()
    }
