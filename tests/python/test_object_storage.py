import base64
import hashlib
import io
import unittest

from botocore.exceptions import ClientError

from services.shared.object_storage import (
    CAPABILITY_PROBE_BYTES,
    ObjectConflictError,
    ObjectDigest,
    ObjectIntegrityError,
    ObjectStorageSettings,
    ObjectTooLargeError,
    OriginalObjectStorage,
    StoredObject,
    hash_stream,
    original_object_key,
    pdf_layout_object_key,
)


def client_error(code: str, operation: str = "PutObject") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, operation)


def digest_for(content: bytes) -> ObjectDigest:
    raw = hashlib.sha256(content).digest()
    return ObjectDigest(len(content), raw.hex(), base64.b64encode(raw).decode("ascii"))


class FakeBody(io.BytesIO):
    def iter_chunks(self, chunk_size: int):
        while chunk := self.read(chunk_size):
            yield chunk


class FakeS3Client:
    def __init__(self):
        self.versioning = "Enabled"
        self.put_calls = []
        self.head_calls = []
        self.get_calls = []
        self.objects = {}
        self.put_error = None
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def head_bucket(self, **kwargs):
        return {}

    def get_bucket_versioning(self, **kwargs):
        return {"Status": self.versioning}

    def put_bucket_versioning(self, **kwargs):
        self.versioning = kwargs["VersioningConfiguration"]["Status"]
        return {}

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs.copy())
        if self.put_error:
            raise self.put_error
        content = kwargs["Body"].read()
        version_id = "version-1"
        self.objects[(kwargs["Key"], version_id)] = (content, kwargs["ChecksumSHA256"])
        return {"VersionId": version_id, "ETag": '"opaque-etag"', "StorageClass": "STANDARD"}

    def head_object(self, **kwargs):
        self.head_calls.append(kwargs.copy())
        version_id = kwargs.get("VersionId", "version-1")
        found = self.objects.get((kwargs["Key"], version_id))
        if not found:
            raise client_error("NoSuchKey", "HeadObject")
        content, checksum = found
        return {
            "VersionId": version_id,
            "ContentLength": len(content),
            "ChecksumSHA256": checksum,
            "ETag": '"opaque-etag"',
        }

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs.copy())
        found = self.objects.get((kwargs["Key"], kwargs["VersionId"]))
        if not found:
            raise client_error("NoSuchVersion", "GetObject")
        return {"Body": FakeBody(found[0]), "ChecksumSHA256": found[1]}


class ObjectStoragePrimitiveTests(unittest.TestCase):
    def test_hash_stream_is_bounded_rewound_and_uses_raw_digest_base64(self):
        content = b"immutable original"
        stream = io.BytesIO(content)

        digest = hash_stream(stream, len(content))

        self.assertEqual(digest, digest_for(content))
        self.assertEqual(stream.tell(), 0)
        self.assertEqual(len(digest.checksum_sha256_base64), 44)

        with self.assertRaises(ObjectTooLargeError):
            hash_stream(io.BytesIO(content), len(content) - 1)

    def test_object_key_is_opaque_scoped_and_version_addressed(self):
        key = original_object_key(
            "tenant@example.com",
            "baf8e16f-c03f-43b0-9738-1fd026bb13db",
            "c2e3ad42-3c79-45ab-b323-d3b88f95a802",
            "a" * 64,
        )

        self.assertTrue(key.startswith("originals/v1/"))
        self.assertNotIn("tenant@example.com", key)
        self.assertIn("baf8e16f-c03f-43b0-9738-1fd026bb13db", key)
        self.assertIn("c2e3ad42-3c79-45ab-b323-d3b88f95a802", key)

    def test_pdf_layout_key_is_opaque_source_and_artifact_addressed(self):
        key = pdf_layout_object_key(
            "tenant@example.com",
            "baf8e16f-c03f-43b0-9738-1fd026bb13db",
            "c2e3ad42-3c79-45ab-b323-d3b88f95a802",
            "a" * 64,
            "b" * 64,
        )

        self.assertTrue(key.startswith("layouts/pdf/v1/"))
        self.assertNotIn("tenant@example.com", key)
        self.assertIn("/" + "a" * 64 + "/", key)
        self.assertTrue(key.endswith("b" * 64 + ".json.gz"))


class OriginalObjectStorageTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeS3Client()
        self.storage = OriginalObjectStorage(
            ObjectStorageSettings(bucket="certus-originals", require_versioning=True),
            client=self.client,
        )

    def test_shared_injected_client_is_closed_exactly_once(self):
        self.storage.close()

        self.assertEqual(self.client.close_calls, 1)

    def test_put_is_conditional_checksummed_and_exactly_verified(self):
        content = b"source bytes"
        digest = digest_for(content)

        stored = self.storage.put_create_once(
            "originals/v1/key",
            io.BytesIO(content),
            digest,
            "application/pdf",
            {"certus-schema": "original-object-v1"},
        )

        put = self.client.put_calls[0]
        self.assertEqual(put["IfNoneMatch"], "*")
        self.assertEqual(put["ChecksumAlgorithm"], "SHA256")
        self.assertEqual(put["ChecksumSHA256"], digest.checksum_sha256_base64)
        self.assertEqual(self.client.head_calls[0]["VersionId"], "version-1")
        self.assertEqual(stored.object_version_id, "version-1")
        self.assertEqual(stored.etag, '"opaque-etag"')

    def test_precondition_failure_only_adopts_matching_existing_bytes(self):
        content = b"same bytes"
        digest = digest_for(content)
        self.client.objects[("key", "version-1")] = (content, digest.checksum_sha256_base64)
        self.client.put_error = client_error("PreconditionFailed")

        adopted = self.storage.put_create_once(
            "key", io.BytesIO(content), digest, "text/plain", {}
        )
        self.assertEqual(adopted.object_version_id, "version-1")

        different = digest_for(b"different")
        with self.assertRaises(ObjectConflictError):
            self.storage.put_create_once(
                "key", io.BytesIO(b"different"), different, "text/plain", {}
            )

    def test_recovery_adopts_a_matching_object_without_rewriting_it(self):
        content = b"already stored"
        digest = digest_for(content)
        self.client.objects[("key", "version-1")] = (content, digest.checksum_sha256_base64)

        recovered = self.storage.recover_existing("key", digest)

        self.assertEqual(recovered.object_version_id, "version-1")
        self.assertEqual(self.client.put_calls, [])

    def test_verified_download_uses_exact_version_and_rehashes_bytes(self):
        content = b"download proof"
        digest = digest_for(content)
        self.client.objects[("key", "version-7")] = (content, digest.checksum_sha256_base64)
        stored = StoredObject(
            bucket="certus-originals",
            object_key="key",
            object_version_id="version-7",
            byte_length=len(content),
            sha256_hex=digest.sha256_hex,
            checksum_sha256_base64=digest.checksum_sha256_base64,
            etag=None,
            storage_class=None,
            server_side_encryption=None,
            kms_key_id=None,
            bucket_key_enabled=None,
        )

        spool = self.storage.download_verified_to_spool(stored)
        try:
            self.assertEqual(spool.read(), content)
        finally:
            spool.close()
        self.assertEqual(self.client.get_calls[0]["VersionId"], "version-7")

        self.client.objects[("key", "version-7")] = (b"tampered", digest.checksum_sha256_base64)
        with self.assertRaises(ObjectIntegrityError):
            self.storage.download_verified_to_spool(stored)

    def test_capability_probe_enables_versioning_and_preserves_probe(self):
        self.client.versioning = None
        self.storage.settings = ObjectStorageSettings(
            bucket="certus-originals",
            auto_create_bucket=True,
            require_versioning=True,
        )

        stored = self.storage.ensure_capabilities()

        self.assertEqual(self.client.versioning, "Enabled")
        self.assertEqual(stored.byte_length, len(CAPABILITY_PROBE_BYTES))

    def test_readiness_rechecks_the_immutable_capability_object(self):
        stored = self.storage.ensure_capabilities()

        self.storage.check_readiness()

        key = stored.object_key
        content, _checksum = self.client.objects[(key, "version-1")]
        self.client.objects[(key, "version-1")] = (content, "invalid-checksum")
        with self.assertRaises(ObjectIntegrityError):
            self.storage.check_readiness()


if __name__ == "__main__":
    unittest.main()
