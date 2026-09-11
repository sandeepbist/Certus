import unittest
from unittest.mock import MagicMock, patch

from services.embedding.app import worker as embedding_worker
from services.shared.embedding_generation_worker import (
    EmbeddingGenerationCandidate,
    EmbeddingGenerationLeaseLostError,
    claim_next_embedding_generation_batch,
    qualify_next_embedding_generation,
    record_embedding_generation_batch,
    record_embedding_generation_failure,
)
from services.shared.embeddings import EMBEDDING_DIMENSIONS, LOCAL_EMBEDDING_PROFILE


GENERATION_ID = "10000000-0000-4000-8000-000000000001"
CHUNK_ID = "20000000-0000-4000-8000-000000000001"
LEASE_OWNER = "30000000-0000-4000-8000-000000000001"


def candidate(attempt_count: int = 1) -> EmbeddingGenerationCandidate:
    return EmbeddingGenerationCandidate(
        generation_id=GENERATION_ID,
        chunk_id=CHUNK_ID,
        content="source text",
        contextualized_content="contextualized source text",
        content_sha256="a" * 64,
        attempt_count=attempt_count,
    )


class EmbeddingGenerationWorkerPrimitiveTests(unittest.TestCase):
    def test_qualification_selects_only_complete_profile_scoped_work(self):
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            (GENERATION_ID, "tenant-proof", "user-proof"),
            (True,),
        ]

        qualified = qualify_next_embedding_generation(
            cursor,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
        )

        self.assertEqual(qualified, GENERATION_ID)
        select_sql, select_params = cursor.execute.call_args_list[0].args
        self.assertIn("embedded_chunk_count = expected_chunk_count", select_sql)
        self.assertEqual(select_params, (LOCAL_EMBEDDING_PROFILE.identifier,))
        self.assertIn(
            "qualify_workspace_embedding_generation",
            cursor.execute.call_args_list[2].args[0],
        )
        self.assertIn("pg_advisory_xact_lock", cursor.execute.call_args_list[1].args[0])

    def test_claim_is_profile_scoped_fair_and_owner_fenced(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (GENERATION_ID, CHUNK_ID, "text", None, "b" * 64, 2)
        ]

        claimed = claim_next_embedding_generation_batch(
            cursor,
            embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
            lease_owner=LEASE_OWNER,
            batch_size=20,
            lease_seconds=300,
        )

        sql, params = cursor.execute.call_args.args
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertIn("claim_chunk_embedding_vectors", sql)
        self.assertIn("ORDER BY generation.updated_at, generation.id", sql)
        self.assertEqual(params[0], LOCAL_EMBEDDING_PROFILE.identifier)
        self.assertEqual(params[2], LEASE_OWNER)
        self.assertEqual(claimed[0].attempt_count, 2)
        self.assertEqual(claimed[0].embedding_input, "text")

    def test_success_batch_requires_every_lease_and_one_generation(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        vector = [0.0] * EMBEDDING_DIMENSIONS

        record_embedding_generation_batch(
            cursor,
            candidates=[candidate()],
            lease_owner=LEASE_OWNER,
            embeddings=[vector],
            provider_metadata={"profile": LOCAL_EMBEDDING_PROFILE.identifier},
        )

        self.assertIn("record_chunk_embedding_vector", cursor.execute.call_args.args[0])
        self.assertIn(LOCAL_EMBEDDING_PROFILE.identifier, cursor.execute.call_args.args[1][4])

        cursor.fetchone.return_value = (False,)
        with self.assertRaises(EmbeddingGenerationLeaseLostError):
            record_embedding_generation_batch(
                cursor,
                candidates=[candidate()],
                lease_owner=LEASE_OWNER,
                embeddings=[vector],
                provider_metadata={},
            )

        with self.assertRaises(ValueError):
            record_embedding_generation_batch(
                cursor,
                candidates=[candidate()],
                lease_owner=LEASE_OWNER,
                embeddings=[[float("nan")] * EMBEDDING_DIMENSIONS],
                provider_metadata={},
            )

    def test_failure_batch_backoffs_and_exhausts_generation(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        cursor.rowcount = 1

        exhausted = record_embedding_generation_failure(
            cursor,
            candidates=[candidate(attempt_count=5)],
            lease_owner=LEASE_OWNER,
            error_code="embedding_provider_error",
            error_message="provider unavailable",
            retry_delay_seconds=120,
            max_attempts=5,
        )

        self.assertTrue(exhausted)
        calls = cursor.execute.call_args_list
        self.assertIn("record_chunk_embedding_failure", calls[0].args[0])
        self.assertIn("status = 'failed'", calls[1].args[0])
        self.assertEqual(calls[1].args[1][2], 5)

    def test_rejects_invalid_worker_bounds_before_sql(self):
        cursor = MagicMock()
        with self.assertRaises(ValueError):
            claim_next_embedding_generation_batch(
                cursor,
                embedding_profile=LOCAL_EMBEDDING_PROFILE.identifier,
                lease_owner=LEASE_OWNER,
                batch_size=101,
                lease_seconds=300,
            )
        cursor.execute.assert_not_called()


class EmbeddingGenerationWorkerLoopTests(unittest.TestCase):
    def test_retention_uses_the_bounded_database_owned_policy(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (2, 3)

        result = embedding_worker.prune_terminal_embedding_generations(connection)

        self.assertEqual(result, (2, 3))
        sql, params = cursor.execute.call_args.args
        self.assertIn("prune_embedding_generations", sql)
        self.assertEqual(
            params,
            (
                embedding_worker.EMBEDDING_GENERATION_RETENTION_DAYS,
                embedding_worker.EMBEDDING_GENERATION_PRUNE_BATCH_SIZE,
            ),
        )
        connection.commit.assert_called_once()

    def test_processes_one_database_batch_with_the_configured_profile(self):
        connection = MagicMock()
        claimed = [candidate()]
        vector = [0.0] * EMBEDDING_DIMENSIONS
        with (
            patch.object(
                embedding_worker,
                "qualify_next_embedding_generation",
                return_value=None,
            ),
            patch.object(
                embedding_worker,
                "claim_next_embedding_generation_batch",
                return_value=claimed,
            ) as claim,
            patch.object(
                embedding_worker,
                "get_embeddings",
                return_value=([vector], LOCAL_EMBEDDING_PROFILE.model),
            ) as embed,
            patch.object(
                embedding_worker,
                "record_embedding_generation_batch",
            ) as record,
        ):
            worked = embedding_worker.process_one_embedding_generation_batch(
                connection
            )

        self.assertTrue(worked)
        self.assertEqual(connection.commit.call_count, 2)
        claim.assert_called_once()
        embed.assert_called_once_with(
            ["contextualized source text"],
            embedding_worker.ACTIVE_EMBEDDING_PROFILE.identifier,
        )
        record.assert_called_once()

    def test_provider_failure_is_durably_released_with_bounded_backoff(self):
        connection = MagicMock()
        claimed = [candidate(attempt_count=3)]
        with (
            patch.object(
                embedding_worker,
                "qualify_next_embedding_generation",
                return_value=None,
            ),
            patch.object(
                embedding_worker,
                "claim_next_embedding_generation_batch",
                return_value=claimed,
            ),
            patch.object(
                embedding_worker,
                "get_embeddings",
                side_effect=RuntimeError("provider unavailable"),
            ),
            patch.object(
                embedding_worker,
                "record_embedding_generation_failure",
                return_value=False,
            ) as record_failure,
        ):
            worked = embedding_worker.process_one_embedding_generation_batch(
                connection
            )

        self.assertTrue(worked)
        self.assertEqual(connection.commit.call_count, 2)
        self.assertEqual(
            record_failure.call_args.kwargs["retry_delay_seconds"],
            min(
                embedding_worker.EMBEDDING_GENERATION_RETRY_MAX_SECONDS,
                embedding_worker.EMBEDDING_GENERATION_RETRY_BASE_SECONDS * 4,
            ),
        )

    def test_stale_provider_result_is_discarded_without_overwriting(self):
        connection = MagicMock()
        vector = [0.0] * EMBEDDING_DIMENSIONS
        with (
            patch.object(
                embedding_worker,
                "qualify_next_embedding_generation",
                return_value=None,
            ),
            patch.object(
                embedding_worker,
                "claim_next_embedding_generation_batch",
                return_value=[candidate()],
            ),
            patch.object(
                embedding_worker,
                "get_embeddings",
                return_value=([vector], LOCAL_EMBEDDING_PROFILE.model),
            ),
            patch.object(
                embedding_worker,
                "record_embedding_generation_batch",
                side_effect=EmbeddingGenerationLeaseLostError("stale"),
            ),
        ):
            worked = embedding_worker.process_one_embedding_generation_batch(
                connection
            )

        self.assertTrue(worked)
        connection.rollback.assert_called_once()

    def test_qualifies_complete_generation_without_provider_work(self):
        connection = MagicMock()
        with (
            patch.object(
                embedding_worker,
                "qualify_next_embedding_generation",
                return_value=GENERATION_ID,
            ),
            patch.object(
                embedding_worker,
                "claim_next_embedding_generation_batch",
            ) as claim,
            patch.object(embedding_worker, "get_embeddings") as embed,
        ):
            worked = embedding_worker.process_one_embedding_generation_batch(
                connection
            )

        self.assertTrue(worked)
        connection.commit.assert_called_once()
        claim.assert_not_called()
        embed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
