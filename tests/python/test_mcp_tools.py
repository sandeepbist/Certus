import asyncio
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import psycopg2


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "services" / "mcp-tools"))

from app import server as server_module
from app import tool_service
from app.server import mcp
from app.tool_service import (
    DATABASE_URL,
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_LOCK_TIMEOUT_MS,
    DB_STATEMENT_TIMEOUT_MS,
    NEO4J_ACQUISITION_TIMEOUT_SECONDS,
    NEO4J_CONNECT_TIMEOUT_SECONDS,
    NEO4J_POOL_SIZE,
    NEO4J_QUERY_TIMEOUT_SECONDS,
    TemporalClientProvider,
    ToolInputError,
    ToolUnavailableError,
    close_graph_driver,
    database_connection,
    database_is_ready,
    extractive_summary,
    graph_driver,
    normalize_tags,
    parse_reminder_time,
    schedule_reminder,
)


class MCPToolContractTests(unittest.TestCase):
    def test_database_connections_are_bounded_and_identified(self):
        with patch.object(database_connection.__globals__["psycopg2"], "connect") as connect:
            database_connection()

        connect.assert_called_once_with(
            DATABASE_URL,
            connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
            application_name="certus-mcp-tools",
            options=(
                f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={DB_LOCK_TIMEOUT_MS}"
            ),
        )

    def test_graph_driver_is_bounded_reused_and_closed(self):
        fake_driver = Mock()
        close_graph_driver()
        try:
            with patch.object(
                tool_service.GraphDatabase,
                "driver",
                return_value=fake_driver,
            ) as create_driver:
                self.assertIs(graph_driver(), fake_driver)
                self.assertIs(graph_driver(), fake_driver)

            create_driver.assert_called_once_with(
                tool_service.NEO4J_URI,
                auth=(tool_service.NEO4J_USER, tool_service.NEO4J_PASSWORD),
                connection_timeout=NEO4J_CONNECT_TIMEOUT_SECONDS,
                connection_acquisition_timeout=NEO4J_ACQUISITION_TIMEOUT_SECONDS,
                max_connection_pool_size=NEO4J_POOL_SIZE,
                max_transaction_retry_time=NEO4J_QUERY_TIMEOUT_SECONDS,
            )
        finally:
            close_graph_driver()
        fake_driver.close.assert_called_once_with()

    def test_temporal_client_connection_is_single_flight_and_reused(self):
        async def exercise():
            provider = TemporalClientProvider()
            client = Mock()
            with patch.object(
                tool_service.Client,
                "connect",
                new=AsyncMock(return_value=client),
            ) as connect:
                first, second = await asyncio.gather(provider.get(), provider.get())

            self.assertIs(first, client)
            self.assertIs(second, client)
            connect.assert_awaited_once_with(
                tool_service.TEMPORAL_ADDRESS,
                namespace=tool_service.TEMPORAL_NAMESPACE,
            )
            self.assertEqual(provider.status, "ready")

        asyncio.run(exercise())

    def test_reminder_start_has_an_rpc_deadline(self):
        async def exercise():
            connection = MagicMock()
            connection.__enter__.return_value = connection
            cursor = MagicMock()
            connection.cursor.return_value.__enter__.return_value = cursor
            handle = Mock(first_execution_run_id="run-1")
            client = Mock()
            client.start_workflow = AsyncMock(return_value=handle)

            with patch.object(
                tool_service,
                "database_connection",
                return_value=connection,
            ), patch.object(
                tool_service.temporal_clients,
                "get",
                new=AsyncMock(return_value=client),
            ), patch.object(
                tool_service.temporal_clients,
                "record_start",
            ) as record_start:
                result = await schedule_reminder(
                    message="Review the evidence",
                    trigger_time="in 15 minutes",
                    identity=tool_service.RequestIdentity("user-1", "tenant-1"),
                )

            self.assertEqual(result["status"], "scheduled")
            call = client.start_workflow.await_args
            self.assertEqual(
                call.kwargs["rpc_timeout"],
                timedelta(seconds=tool_service.TEMPORAL_RPC_TIMEOUT_SECONDS),
            )
            record_start.assert_called_once_with(True)

        asyncio.run(exercise())

    def test_blocking_tool_capacity_rejects_instead_of_queueing_without_bound(self):
        async def exercise():
            entered = threading.Event()
            release = threading.Event()

            def occupy():
                entered.set()
                release.wait(2)
                return "finished"

            with patch.object(server_module, "blocking_tool_slots", asyncio.Semaphore(1)), patch.object(
                server_module,
                "BLOCKING_TOOL_QUEUE_TIMEOUT_MS",
                20,
            ):
                first = asyncio.create_task(server_module.run_blocking_tool(occupy))
                await asyncio.to_thread(entered.wait, 1)
                with self.assertRaises(ToolUnavailableError):
                    await server_module.run_blocking_tool(lambda: "must-not-run")
                release.set()
                self.assertEqual(await first, "finished")

        asyncio.run(exercise())

    def test_database_readiness_fails_closed(self):
        unavailable = Mock(side_effect=psycopg2.OperationalError("unavailable"))
        with patch.dict(
            database_is_ready.__globals__,
            {"database_connection": unavailable},
        ):
            result = database_is_ready()

        self.assertFalse(result)

    def test_registry_is_derived_from_valid_mcp_tool_schemas(self):
        tools = asyncio.run(mcp.list_tools())
        by_name = {tool.name: tool for tool in tools}

        self.assertEqual(
            set(by_name),
            {
                "search_notes",
                "create_task",
                "summarize_document",
                "schedule_reminder",
                "graph_query",
            },
        )
        self.assertEqual(by_name["search_notes"].input_schema["type"], "object")
        self.assertIn("query", by_name["search_notes"].input_schema["required"])
        self.assertNotIn("context", by_name["search_notes"].input_schema["properties"])
        self.assertIn("retrieval_mode", by_name["search_notes"].output_schema["properties"])
        self.assertNotIn("result", by_name["search_notes"].output_schema["properties"])
        self.assertTrue(by_name["search_notes"].annotations.read_only_hint)
        self.assertFalse(by_name["create_task"].annotations.read_only_hint)

    def test_tag_normalization_is_bounded_and_deduplicated(self):
        self.assertEqual(normalize_tags([" Urgent ", "urgent", "Audit"]), ["urgent", "audit"])
        with self.assertRaises(ToolInputError):
            normalize_tags([str(index) for index in range(21)])

    def test_reminder_time_parser_requires_bounded_timezone_aware_values(self):
        now = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            parse_reminder_time("in 15 minutes", now=now),
            datetime(2026, 8, 24, 12, 15, tzinfo=timezone.utc),
        )
        with self.assertRaises(ToolInputError):
            parse_reminder_time("2026-08-24T13:00:00", now=now)
        with self.assertRaises(ToolInputError):
            parse_reminder_time("in 367 days", now=now)

    def test_extractive_summary_is_honest_and_bounded(self):
        summary, points = extractive_summary(
            "First grounded sentence. Second grounded sentence. Third grounded sentence. Fourth grounded sentence.",
            "brief",
        )

        self.assertEqual(len(points), 3)
        self.assertNotIn("Fourth grounded sentence", summary)


if __name__ == "__main__":
    unittest.main()
