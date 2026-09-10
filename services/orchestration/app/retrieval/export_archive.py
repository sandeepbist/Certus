import json
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.encoders import jsonable_encoder


ARCHIVE_ENTRY_NAME = "certus-export.json"


class ExportArchiveLimitExceeded(ValueError):
    """Raised before an export can grow beyond its in-process byte budget."""


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
) -> bytes:
    """Build a ZIP without first materializing a second full JSON copy."""
    encoder = json.JSONEncoder(
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        default=jsonable_encoder,
    )

    output = BytesIO()
    with ZipFile(output, mode="w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        written = 0
        with archive.open(ARCHIVE_ENTRY_NAME, mode="w", force_zip64=True) as entry:
            for fragment in encoder.iterencode(payload):
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
