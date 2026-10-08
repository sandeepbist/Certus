import unittest
from unittest.mock import MagicMock, patch

from services.ingestion.app.extractors import entity_extractor
from services.ingestion.app.extractors.entity_extractor import EntityExtractor, ExtractedEntity


class DocumentGraphTransactionTests(unittest.TestCase):
    operations = (
        (EntityExtractor.sync_to_neo4j,
         ("document", "title", "user", "tenant", [ExtractedEntity("PostgreSQL", "TECHNOLOGY")]), 4),
        (EntityExtractor.mark_document_deleted,
         ("document", "user", "tenant", "2026-10-09T00:00:00Z"), 2),
        (EntityExtractor.restore_document, ("document", "user", "tenant", 1), 2),
    )

    def driver(self):
        driver = MagicMock()
        session = driver.session.return_value.__enter__.return_value
        context = session.begin_transaction.return_value
        transaction = context.__enter__.return_value
        transaction.run.return_value.single.return_value = {"existed": True, "has_mentions": True}
        return driver, session, context, transaction

    def test_lifecycle_commits_only_after_all_writes_in_one_bounded_transaction(self):
        for operation, arguments, query_count in self.operations:
            with self.subTest(operation=operation.__name__):
                driver, session, context, transaction = self.driver()
                with patch.object(entity_extractor, "entity_graph_driver", return_value=driver):
                    result = operation(*arguments)

                session.run.assert_not_called()
                session.begin_transaction.assert_called_once_with(
                    timeout=entity_extractor.NEO4J_QUERY_TIMEOUT_SECONDS,
                )
                self.assertEqual(transaction.run.call_count, query_count)
                transaction.commit.assert_called_once_with()
                self.assertEqual(transaction.method_calls[-1][0], "commit")
                context.__exit__.assert_called_once_with(None, None, None)
                if operation != EntityExtractor.sync_to_neo4j:
                    self.assertEqual(result, "synced")

    def test_every_query_and_commit_failure_closes_transaction_and_reports_degradation(self):
        for operation, arguments, query_count in self.operations:
            for failure_index in range(query_count + 1):
                with self.subTest(operation=operation.__name__, failure_index=failure_index):
                    driver, session, context, transaction = self.driver()
                    failure = RuntimeError("private graph connection details")
                    if failure_index < query_count:
                        result = transaction.run.return_value
                        transaction.run.side_effect = [result] * failure_index + [failure]
                    else:
                        transaction.commit.side_effect = failure

                    with (
                        patch.object(entity_extractor, "entity_graph_driver", return_value=driver),
                        self.assertLogs("entity_extractor", level="WARNING") as captured,
                    ):
                        result = operation(*arguments)

                    session.run.assert_not_called()
                    if failure_index < query_count:
                        transaction.commit.assert_not_called()
                    else:
                        transaction.commit.assert_called_once_with()
                    self.assertIs(context.__exit__.call_args.args[1], failure)
                    self.assertNotIn("private graph connection details", "\n".join(captured.output))
                    if operation != EntityExtractor.sync_to_neo4j:
                        self.assertEqual(result, "degraded")


if __name__ == "__main__":
    unittest.main()
