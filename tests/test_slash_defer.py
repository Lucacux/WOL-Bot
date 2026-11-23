"""Discord da 3 s para responder una interacción y el sondeo de estado puede
tardar más (un server de otra VLAN apagado espera el timeout TCP entero). Si un
slash-command sondea antes de acusar recibo, Discord muestra "The application
did not respond" y el panel nunca llega. Estos tests fijan el orden."""
import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import main


def _fake_interaction(calls):
    inter = MagicMock()
    inter.response.defer = AsyncMock(side_effect=lambda **kw: calls.append("defer"))
    inter.response.send_message = AsyncMock(side_effect=lambda *a, **kw: calls.append("send_message"))
    inter.followup.send = AsyncMock(side_effect=lambda *a, **kw: calls.append("followup"))
    return inter


async def _slow_statuses(calls):
    calls.append("probe")
    return {k: False for k in main.config.SERVERS}


class SlashCommandsDeferFirst(unittest.TestCase):
    def _run(self, command):
        calls = []
        inter = _fake_interaction(calls)
        with patch("main.gather_statuses", new=lambda: _slow_statuses(calls)):
            asyncio.run(command.callback(inter))
        return calls, inter

    def test_commands_defer_before_probing(self):
        for command in (main.wol_command, main.reboot_command, main.shutdown_command):
            with self.subTest(command=command.name):
                calls, inter = self._run(command)
                self.assertEqual(calls, ["defer", "probe", "followup"])
                inter.response.send_message.assert_not_awaited()
                kwargs = inter.followup.send.await_args.kwargs
                self.assertIn("embed", kwargs)
                self.assertIn("view", kwargs)


if __name__ == "__main__":
    unittest.main()
