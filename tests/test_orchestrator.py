import unittest
from unittest.mock import AsyncMock, patch

from orchestrator import ensure_online


class EnsureOnlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_already_online_does_not_send_wol(self):
        with (
            patch("orchestrator.check_status", new=AsyncMock(return_value=True)),
            patch("orchestrator.wake", new=AsyncMock()) as wake,
        ):
            result = await ensure_online("nas", boot_grace=0)
        self.assertTrue(result.ready)
        self.assertTrue(result.already_online)
        wake.assert_not_awaited()

    async def test_wakes_and_confirms_after_grace(self):
        statuses = AsyncMock(side_effect=[False, True, True])
        with (
            patch("orchestrator.check_status", new=statuses),
            patch("orchestrator.wake", new=AsyncMock(return_value=True)) as wake,
        ):
            result = await ensure_online(
                "media", attempts=3, attempt_timeout=0, boot_grace=0
            )
        self.assertTrue(result.ready)
        self.assertFalse(result.already_online)
        self.assertEqual(result.wol_attempts, 1)
        wake.assert_awaited_once_with("media")

    async def test_exhausts_bounded_attempts(self):
        with (
            patch("orchestrator.check_status", new=AsyncMock(return_value=False)),
            patch("orchestrator.wake", new=AsyncMock(return_value=True)) as wake,
        ):
            result = await ensure_online(
                "nas", attempts=3, attempt_timeout=0, boot_grace=0
            )
        self.assertFalse(result.ready)
        self.assertEqual(result.wol_attempts, 3)
        self.assertEqual(wake.await_count, 3)
