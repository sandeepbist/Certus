import math
import unittest
from unittest.mock import MagicMock, patch

from services.embedding.app import worker as embedding_worker
from services.embedding.app.worker import _validated_job_fields
from services.shared.embeddings import (
    EMBEDDING_DIMENSIONS,
    LEGACY_EMBEDDING_PROFILE,
    LOCAL_EMBEDDING_PROFILE,
    configured_embedding_profile,
    local_lexical_embedding,
    parse_embedding_profile,
    EmbeddingProfile,
)
from services.shared.embedding_registry import (
    embedding_profile_record,
    register_embedding_profile,
)


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


class LocalLexicalEmbeddingTests(unittest.TestCase):
    def test_is_deterministic_normalized_and_schema_compatible(self):
        first = local_lexical_embedding("Certus verifies tenant boundaries")
        second = local_lexical_embedding("Certus verifies tenant boundaries")

        self.assertEqual(first, second)
        self.assertEqual(len(first), EMBEDDING_DIMENSIONS)
        self.assertAlmostEqual(math.sqrt(sum(value * value for value in first)), 1.0, places=9)

    def test_related_text_scores_above_unrelated_text(self):
        document = local_lexical_embedding("Tenant isolation protects every workspace document")
        related = local_lexical_embedding("workspace tenant isolation")
        unrelated = local_lexical_embedding("banana recipes and tropical weather")

        self.assertGreater(
            cosine_similarity(document, related),
            cosine_similarity(document, unrelated),
        )


class EmbeddingProfileTests(unittest.TestCase):
    def test_local_profile_is_stable_without_a_usable_provider_key(self):
        for api_key in ("", "placeholder-not-a-key", "test-key"):
            profile = configured_embedding_profile(api_key)
            self.assertEqual(profile, LOCAL_EMBEDDING_PROFILE)
            self.assertEqual(
                profile.identifier,
                "embedding-space:v1:local:local-lexical-v2:1536",
            )

    def test_supported_openai_model_gets_a_distinct_coordinate_space(self):
        profile = configured_embedding_profile("sk-live", "text-embedding-3-large")

        self.assertEqual(profile.provider, "openai")
        self.assertEqual(
            profile.identifier,
            "embedding-space:v1:openai:text-embedding-3-large:1536",
        )
        self.assertEqual(parse_embedding_profile(profile.identifier), profile)

    def test_unsupported_or_noncanonical_profiles_fail_closed(self):
        with self.assertRaises(ValueError):
            configured_embedding_profile("sk-live", "text-embedding-ada-002")
        with self.assertRaises(ValueError):
            parse_embedding_profile("embedding-space:v1:local:unsafe model:1536")
        with self.assertRaises(ValueError):
            parse_embedding_profile("embedding-space:v1:local:local-lexical-v2:768")
        with self.assertRaises(ValueError):
            EmbeddingProfile(provider="local", model="x" * 121)

    def test_worker_rejects_a_different_space_without_calling_a_provider(self):
        with self.assertRaises(embedding_worker.EmbeddingProfileMismatchError):
            embedding_worker.get_embeddings(
                ["profile isolation"],
                LEGACY_EMBEDDING_PROFILE.identifier,
            )

    def test_registry_record_preserves_the_complete_vector_contract(self):
        record = embedding_profile_record(LOCAL_EMBEDDING_PROFILE)

        self.assertEqual(record["identifier"], LOCAL_EMBEDDING_PROFILE.identifier)
        self.assertEqual(record["dimensions"], EMBEDDING_DIMENSIONS)
        self.assertEqual(record["distance_metric"], "cosine")
        self.assertEqual(record["normalization_profile"], "l2_normalized:v1")
        self.assertEqual(record["input_profile"], "plain_text_unprefixed:v1")
        self.assertEqual(record["storage_profile"], "pgvector_float32_cosine:v1")

    def test_profile_registration_is_idempotent_and_parameterized(self):
        cursor = MagicMock()

        register_embedding_profile(cursor, LOCAL_EMBEDDING_PROFILE)

        sql, parameters = cursor.execute.call_args.args
        self.assertIn("ON CONFLICT (identifier) DO NOTHING", sql)
        self.assertEqual(parameters[0], LOCAL_EMBEDDING_PROFILE.identifier)
        self.assertNotIn(LOCAL_EMBEDDING_PROFILE.identifier, sql)

    def test_provider_client_is_reused_with_bounded_timeout_and_retries(self):
        client = MagicMock()
        with (
            patch("openai.OpenAI", return_value=client) as constructor,
            patch.object(embedding_worker, "_openai_client", None),
            patch.object(embedding_worker, "OPENAI_REQUEST_TIMEOUT_SECONDS", 17),
            patch.object(embedding_worker, "OPENAI_MAX_RETRIES", 1),
        ):
            first = embedding_worker._get_openai_client()
            second = embedding_worker._get_openai_client()

        self.assertIs(first, client)
        self.assertIs(second, client)
        constructor.assert_called_once_with(
            api_key=embedding_worker.OPENAI_API_KEY,
            timeout=17,
            max_retries=1,
        )


class EmbeddingJobContractTests(unittest.TestCase):
    def test_accepts_a_generation_scoped_outbox_job(self):
        parsed = _validated_job_fields({
            "outbox_id": "10000000-0000-0000-0000-000000000001",
            "document_id": "20000000-0000-0000-0000-000000000001",
            "document_version_id": "21000000-0000-0000-0000-000000000001",
            "derivation_id": "22000000-0000-0000-0000-000000000001",
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "processing_generation": "30000000-0000-0000-0000-000000000001",
            "embedding_profile": LOCAL_EMBEDDING_PROFILE.identifier,
            "batch_start": "20",
            "batch_end": "40",
            "total_chunks": "45",
        })

        self.assertEqual(parsed[7], LOCAL_EMBEDDING_PROFILE.identifier)
        self.assertEqual(parsed[8:], (20, 40, 45))

    def test_rejects_invalid_embedding_job_boundaries(self):
        with self.assertRaises(ValueError):
            _validated_job_fields({
                "outbox_id": "10000000-0000-0000-0000-000000000001",
                "document_id": "20000000-0000-0000-0000-000000000001",
                "document_version_id": "21000000-0000-0000-0000-000000000001",
                "derivation_id": "22000000-0000-0000-0000-000000000001",
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "processing_generation": "30000000-0000-0000-0000-000000000001",
                "embedding_profile": LOCAL_EMBEDDING_PROFILE.identifier,
                "batch_start": "40",
                "batch_end": "50",
                "total_chunks": "45",
            })

    def test_rejects_malformed_embedding_job_identity(self):
        with self.assertRaises(ValueError):
            _validated_job_fields({
                "outbox_id": "not-a-uuid",
                "document_id": "20000000-0000-0000-0000-000000000001",
                "document_version_id": "21000000-0000-0000-0000-000000000001",
                "derivation_id": "22000000-0000-0000-0000-000000000001",
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "processing_generation": "30000000-0000-0000-0000-000000000001",
                "batch_start": "0",
                "batch_end": "20",
                "total_chunks": "20",
            })

    def test_rejects_a_noncanonical_embedding_profile(self):
        with self.assertRaises(ValueError):
            _validated_job_fields({
                "outbox_id": "10000000-0000-0000-0000-000000000001",
                "document_id": "20000000-0000-0000-0000-000000000001",
                "document_version_id": "21000000-0000-0000-0000-000000000001",
                "derivation_id": "22000000-0000-0000-0000-000000000001",
                "tenant_id": "tenant-a",
                "user_id": "user-a",
                "processing_generation": "30000000-0000-0000-0000-000000000001",
                "embedding_profile": "local-lexical-v2",
                "batch_start": "0",
                "batch_end": "20",
                "total_chunks": "20",
            })

    def test_profile_mismatch_is_not_retried(self):
        connection = object()
        redis_client = MagicMock()
        mismatch = embedding_worker.EmbeddingProfileMismatchError("wrong space")

        with (
            patch.object(embedding_worker, "MAX_RETRIES", 3),
            patch.object(embedding_worker, "process_batch", side_effect=mismatch) as process,
            patch.object(
                embedding_worker,
                "reconnect_database",
                return_value=connection,
            ),
            patch.object(embedding_worker, "mark_document_failed") as mark_failed,
            patch.object(embedding_worker, "acknowledge_message") as acknowledge,
            patch.object(embedding_worker.time, "sleep") as sleep,
        ):
            result = embedding_worker.process_message(
                connection,
                redis_client,
                "3-0",
                {"outbox_id": "job"},
            )

        self.assertIs(result, connection)
        process.assert_called_once()
        sleep.assert_not_called()
        mark_failed.assert_called_once()
        redis_client.xadd.assert_called_once()
        acknowledge.assert_called_once_with(redis_client, "3-0")

    def test_releases_its_lease_before_an_immediate_retry(self):
        connection = object()
        redis_client = object()
        attempts = []

        def process_once_then_succeed(
            conn, client, message_id, fields, processing_owner, lease_state
        ):
            attempts.append(processing_owner)
            if len(attempts) == 1:
                lease_state["acquired"] = True
                raise RuntimeError("provider unavailable")

        with (
            patch.object(embedding_worker, "MAX_RETRIES", 2),
            patch.object(
                embedding_worker,
                "process_batch",
                side_effect=process_once_then_succeed,
            ),
            patch.object(
                embedding_worker,
                "release_processing_lease",
                return_value=True,
            ) as release_lease,
            patch.object(
                embedding_worker,
                "reconnect_database",
                return_value=connection,
            ),
            patch.object(embedding_worker.time, "sleep"),
        ):
            result = embedding_worker.process_message(
                connection,
                redis_client,
                "1-0",
                {},
            )

        self.assertIs(result, connection)
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(attempts[0], attempts[1])
        release_lease.assert_called_once()

    def test_discards_a_stale_delivery_after_losing_its_lease(self):
        connection = object()
        redis_client = object()

        def lose_lease(conn, client, message_id, fields, processing_owner, lease_state):
            lease_state["acquired"] = True
            raise RuntimeError("late provider failure")

        with (
            patch.object(embedding_worker, "MAX_RETRIES", 3),
            patch.object(embedding_worker, "process_batch", side_effect=lose_lease),
            patch.object(
                embedding_worker,
                "release_processing_lease",
                return_value=False,
            ),
            patch.object(embedding_worker, "acknowledge_message") as acknowledge,
        ):
            result = embedding_worker.process_message(
                connection,
                redis_client,
                "2-0",
                {},
            )

        self.assertIs(result, connection)
        acknowledge.assert_called_once_with(redis_client, "2-0")


if __name__ == "__main__":
    unittest.main()
