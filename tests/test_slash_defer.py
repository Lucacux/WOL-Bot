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


class ButtonsDeferFirst(unittest.TestCase):
    """Mismo plazo de 3 s para los botones: el de despertar sondeaba antes de
    responder y con el pve apagado tiraba "Unknown interaction"."""

    def _inter(self, calls):
        inter = MagicMock()
        inter.response.defer = AsyncMock(side_effect=lambda **kw: calls.append("defer"))
        inter.response.send_message = AsyncMock(side_effect=lambda *a, **kw: calls.append("send_message"))
        inter.response.edit_message = AsyncMock(side_effect=lambda *a, **kw: calls.append("edit_message"))
        inter.followup.send = AsyncMock(side_effect=lambda *a, **kw: calls.append("followup"))
        inter.edit_original_response = AsyncMock(side_effect=lambda *a, **kw: calls.append("edit_original"))
        inter.channel.send = AsyncMock(side_effect=lambda *a, **kw: calls.append("channel"))
        return inter

    def test_wake_button_defers_before_probe(self):
        import views
        calls = []
        inter = self._inter(calls)

        async def probe(_):
            calls.append("probe")
            return False

        async def wake(_):
            calls.append("wake")
            return True

        async def run():
            panel = views.WOLPanel({k: False for k in main.config.SERVERS})
            with patch("views.probe", new=probe), patch("views.wake", new=wake), \
                 patch("views.monitor_boot", new=AsyncMock()):
                await panel._make_wake_callback("proxmox")(inter)

        asyncio.run(run())
        self.assertEqual(calls[:4], ["defer", "probe", "wake", "followup"])
        inter.response.send_message.assert_not_awaited()

    def test_confirm_buttons_defer_before_probe(self):
        import views
        for cls in (views.RebootConfirmView, views.ShutdownConfirmView):
            with self.subTest(view=cls.__name__):
                calls = []
                inter = self._inter(calls)

                async def probe(_):
                    calls.append("probe")
                    return False  # offline: rama que antes vencía

                async def run():
                    view = cls("proxmox")
                    with patch("views.probe", new=probe):
                        await view._confirm(inter)

                asyncio.run(run())
                self.assertEqual(calls, ["defer", "probe", "edit_original"])
                inter.response.edit_message.assert_not_awaited()
