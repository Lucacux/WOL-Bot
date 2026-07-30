import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from scheduler import _process_schedule, run_shutdown_warning


class SchedulerMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_active_lease_does_not_consume_shutdown_window(self):
        cfg = {
            "enabled": True,
            "wake_time": "11:30",
            "shutdown_time": "12:30",
            "last_wake_date": "2026-07-30",
            "last_shutdown_date": None,
            "shutdown_cancelled_date": None,
            "failsafe_enabled": True,
        }
        now = datetime(2026, 7, 30, 12, 28)
        lease = SimpleNamespace(owner="updates-bot-daily")

        with patch("scheduler.active_lease", return_value=lease):
            dirty = await _process_schedule(None, "nas", cfg, now)

        self.assertFalse(dirty)
        self.assertIsNone(cfg["last_shutdown_date"])

    async def test_lease_during_countdown_reopens_shutdown_guard(self):
        cfg = {"shutdown_time": "12:30"}
        current = {"last_shutdown_date": "2026-07-30"}
        message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(send=AsyncMock(return_value=message))
        bot = SimpleNamespace(get_channel=lambda _channel_id: channel)
        lease = SimpleNamespace(owner="updates-bot-daily")

        with (
            patch("views.CancelShutdownView", return_value=SimpleNamespace(cancelled=False)),
            patch("scheduler.active_lease", return_value=lease),
            patch("scheduler.load_schedule", return_value=current),
            patch("scheduler.save_schedule") as save_schedule,
        ):
            await run_shutdown_warning(bot, "nas", cfg, "2026-07-30")

        self.assertIsNone(current["last_shutdown_date"])
        save_schedule.assert_called_once_with("nas", current)
        message.edit.assert_awaited_once()
