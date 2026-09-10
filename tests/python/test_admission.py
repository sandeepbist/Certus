import asyncio
import unittest

from services.shared.admission import AdmissionCapacityExceeded, AsyncAdmissionController


class AsyncAdmissionControllerTests(unittest.TestCase):
    def test_full_lane_rejects_within_queue_budget_and_releases_capacity(self):
        async def exercise():
            controller = AsyncAdmissionController(capacity=1, queue_timeout_ms=20)
            entered = asyncio.Event()
            release = asyncio.Event()

            async def occupy():
                async with controller.acquire():
                    entered.set()
                    await release.wait()

            first = asyncio.create_task(occupy())
            await entered.wait()
            with self.assertRaises(AdmissionCapacityExceeded):
                async with controller.acquire():
                    self.fail("full capacity must not admit more work")
            release.set()
            await first
            async with controller.acquire():
                pass

        asyncio.run(exercise())

    def test_invalid_capacity_and_wait_fail_closed(self):
        with self.assertRaises(ValueError):
            AsyncAdmissionController(capacity=0, queue_timeout_ms=100)
        with self.assertRaises(ValueError):
            AsyncAdmissionController(capacity=1, queue_timeout_ms=0)


if __name__ == "__main__":
    unittest.main()
