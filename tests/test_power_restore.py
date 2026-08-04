import unittest
from datetime import datetime, time
from unittest.mock import AsyncMock, patch

from orchestrator import restore_power_state
from schedule_store import in_uptime_window, should_be_online

# Franja de ejemplo: 11:30 → 12:30, la que usa hoy el NAS.
SCHEDULE_ON = {"enabled": True, "wake_time": "11:30", "shutdown_time": "12:30"}
SCHEDULE_OFF = {"enabled": False, "wake_time": "11:30", "shutdown_time": "12:30"}

AT_NIGHT = datetime(2026, 8, 5, 3, 40)
AT_NOON = datetime(2026, 8, 5, 12, 0)


class ShouldBeOnlineTests(unittest.TestCase):
    def test_inside_window(self):
        with patch("schedule_store.load_schedule", return_value=SCHEDULE_ON):
            expected, reason = should_be_online("nas", now=AT_NOON)
        self.assertTrue(expected)
        self.assertIn("11:30", reason)

    def test_outside_window(self):
        with patch("schedule_store.load_schedule", return_value=SCHEDULE_ON):
            expected, _ = should_be_online("nas", now=AT_NIGHT)
        self.assertFalse(expected)

    def test_disabled_schedule_is_not_always_on(self):
        """Sin franja configurada nadie prometió tenerlo encendido."""
        with patch("schedule_store.load_schedule", return_value=SCHEDULE_OFF):
            expected, reason = should_be_online("nas", now=AT_NOON)
        self.assertFalse(expected)
        self.assertIn("deshabilitado", reason)

    def test_unreadable_schedule_keeps_the_server_on(self):
        """Ante un horario roto se elige no apagar: es la falla barata."""
        broken = {"enabled": True, "wake_time": "no-es-una-hora", "shutdown_time": "12:30"}
        with patch("schedule_store.load_schedule", return_value=broken):
            expected, reason = should_be_online("nas", now=AT_NIGHT)
        self.assertTrue(expected)
        self.assertIn("ilegible", reason)

    def test_window_crossing_midnight(self):
        self.assertTrue(in_uptime_window(time(23, 30), time(22, 0), time(6, 0)))
        self.assertTrue(in_uptime_window(time(2, 0), time(22, 0), time(6, 0)))
        self.assertFalse(in_uptime_window(time(12, 0), time(22, 0), time(6, 0)))

    def test_equal_times_mean_always_on(self):
        self.assertTrue(in_uptime_window(time(4, 0), time(9, 0), time(9, 0)))


class RestorePowerStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_shuts_down_when_outside_window(self):
        with (
            patch("orchestrator.active_lease", return_value=None),
            patch("orchestrator.check_status", new=AsyncMock(return_value=True)),
            patch("orchestrator.should_be_online", return_value=(False, "fuera de la franja")),
            patch("orchestrator.ssh_shutdown", new=AsyncMock(return_value=True)) as shutdown,
        ):
            result = await restore_power_state("nas", owner="homelab-backup", now=AT_NIGHT)
        self.assertTrue(result.ok)
        self.assertEqual(result.action, "shutdown")
        shutdown.assert_awaited_once_with("nas")

    async def test_keeps_online_inside_window(self):
        with (
            patch("orchestrator.active_lease", return_value=None),
            patch("orchestrator.check_status", new=AsyncMock(return_value=True)),
            patch("orchestrator.should_be_online", return_value=(True, "dentro de la franja")),
            patch("orchestrator.ssh_shutdown", new=AsyncMock()) as shutdown,
        ):
            result = await restore_power_state("nas", owner="homelab-backup", now=AT_NOON)
        self.assertEqual(result.action, "kept-online")
        shutdown.assert_not_awaited()

    async def test_offline_server_is_left_alone(self):
        with (
            patch("orchestrator.active_lease", return_value=None),
            patch("orchestrator.check_status", new=AsyncMock(return_value=False)),
            patch("orchestrator.should_be_online") as window,
            patch("orchestrator.ssh_shutdown", new=AsyncMock()) as shutdown,
        ):
            result = await restore_power_state("nas", owner="homelab-backup")
        self.assertEqual(result.action, "already-offline")
        window.assert_not_called()
        shutdown.assert_not_awaited()

    async def test_another_owners_lease_blocks_the_shutdown(self):
        lease = unittest.mock.Mock(owner="updates-bot-daily", remaining_seconds=900)
        with (
            patch("orchestrator.active_lease", return_value=lease),
            patch("orchestrator.check_status", new=AsyncMock(return_value=True)),
            patch("orchestrator.ssh_shutdown", new=AsyncMock()) as shutdown,
        ):
            result = await restore_power_state("nas", owner="homelab-backup")
        self.assertEqual(result.action, "blocked")
        self.assertIn("updates-bot-daily", result.reason)
        shutdown.assert_not_awaited()

    async def test_own_lease_does_not_block(self):
        lease = unittest.mock.Mock(owner="homelab-backup", remaining_seconds=900)
        with (
            patch("orchestrator.active_lease", return_value=lease),
            patch("orchestrator.check_status", new=AsyncMock(return_value=True)),
            patch("orchestrator.should_be_online", return_value=(False, "fuera de la franja")),
            patch("orchestrator.ssh_shutdown", new=AsyncMock(return_value=True)),
        ):
            result = await restore_power_state("nas", owner="homelab-backup")
        self.assertEqual(result.action, "shutdown")

    async def test_failed_shutdown_is_reported_as_an_error(self):
        with (
            patch("orchestrator.active_lease", return_value=None),
            patch("orchestrator.check_status", new=AsyncMock(return_value=True)),
            patch("orchestrator.should_be_online", return_value=(False, "fuera de la franja")),
            patch("orchestrator.ssh_shutdown", new=AsyncMock(return_value=False)),
        ):
            result = await restore_power_state("nas", owner="homelab-backup")
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "error")

    async def test_unknown_server(self):
        result = await restore_power_state("no-existe")
        self.assertFalse(result.ok)
        self.assertEqual(result.action, "error")


if __name__ == "__main__":
    unittest.main()
