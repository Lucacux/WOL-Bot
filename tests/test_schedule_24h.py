"""wake_time == shutdown_time ⇒ franja de 24 h: nunca se apaga por horario."""
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import config
from embeds import build_schedule_embed
from scheduler import _process_schedule


def _cfg(wake: str, shut: str, last_wake: str | None = None) -> dict:
    return {
        "enabled": True,
        "wake_time": wake,
        "shutdown_time": shut,
        "last_wake_date": last_wake,
        "last_shutdown_date": None,
        "shutdown_cancelled_date": None,
        "failsafe_enabled": True,
    }


class AlwaysOnSchedule(unittest.IsolatedAsyncioTestCase):
    async def _run(self, cfg: dict, instants, *, online: bool = True):
        """Corre _process_schedule en cada instante; devuelve los mocks."""
        with (
            patch("scheduler.probe", AsyncMock(return_value=online)),
            patch("scheduler.wake", AsyncMock(return_value=True)) as wake,
            patch("scheduler.active_lease", return_value=None),
            patch("scheduler.asyncio.create_task") as create_task,
        ):
            bot = SimpleNamespace(get_channel=lambda _id: None)  # sin canal: no postea
            for now in instants:
                await _process_schedule(bot, "proxmox", cfg, now)
        return wake, create_task

    async def test_midnight_to_midnight_never_shuts_down_in_48h(self):
        cfg = _cfg("00:00", "00:00")
        start = datetime(2026, 11, 23, 0, 0)
        instants = [start + timedelta(seconds=30 * i) for i in range(48 * 120 + 1)]

        _, create_task = await self._run(cfg, instants)

        create_task.assert_not_called()
        self.assertIsNone(cfg["last_shutdown_date"])

    async def test_any_equal_time_is_always_on(self):
        cfg = _cfg("08:00", "08:00", last_wake="2026-11-23")
        # Justo en el aviso y justo en la hora en que antes se apagaba.
        instants = [datetime(2026, 11, 24, 7, 58, 30), datetime(2026, 11, 24, 8, 0)]

        _, create_task = await self._run(cfg, instants)

        create_task.assert_not_called()

    async def test_daily_wake_still_recovers_a_server_found_off(self):
        cfg = _cfg("00:00", "00:00", last_wake="2026-11-22")

        wake, _ = await self._run(cfg, [datetime(2026, 11, 23, 0, 0, 30)], online=False)

        wake.assert_awaited_once_with("proxmox")
        self.assertEqual(cfg["last_wake_date"], "2026-11-23")


class WindowsThatStillShutDown(unittest.IsolatedAsyncioTestCase):
    async def _shutdown_scheduled(self, cfg: dict, now: datetime) -> bool:
        with (
            patch("scheduler.probe", AsyncMock(return_value=True)),
            patch("scheduler.active_lease", return_value=None),
            patch("scheduler.asyncio.create_task") as create_task,
        ):
            await _process_schedule(None, "proxmox", cfg, now)
        create_task.call_args and create_task.call_args.args[0].close()
        return create_task.called

    async def test_normal_window_shuts_down(self):
        cfg = _cfg("08:00", "22:35", last_wake="2026-11-23")
        now = datetime(2026, 11, 23, 22, 35) - timedelta(seconds=config.SHUTDOWN_WARN_SECS)
        self.assertTrue(await self._shutdown_scheduled(cfg, now))

    async def test_window_crossing_midnight_shuts_down_next_day(self):
        cfg = _cfg("22:00", "06:00", last_wake="2026-11-23")
        self.assertFalse(await self._shutdown_scheduled(cfg, datetime(2026, 11, 23, 23, 0)))
        now = datetime(2026, 11, 24, 6, 0) - timedelta(seconds=config.SHUTDOWN_WARN_SECS)
        self.assertTrue(await self._shutdown_scheduled(cfg, now))


class ScheduleEmbed24h(unittest.TestCase):
    def _fields(self, cfg):
        return {f.name: f.value for f in build_schedule_embed("proxmox", cfg).fields}

    def test_equal_times_show_never(self):
        fields = self._fields(_cfg("00:00", "00:00"))
        self.assertIn("Nunca", fields["🌙  Apagado"])
        self.assertIn("cualquier momento", fields["🛟  Failsafe (watchdog WOL)"])

    def test_normal_window_shows_time(self):
        fields = self._fields(_cfg("08:00", "22:35"))
        self.assertEqual(fields["🌙  Apagado"], "`22:35`")


if __name__ == "__main__":
    unittest.main()
