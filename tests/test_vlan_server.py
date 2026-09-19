"""Un servidor en otra VLAN rompe dos supuestos: el broadcast del magic packet
y el ping. Estos tests fijan los dos overrides que lo arreglan, más el comando
remoto por servidor, para que un refactor no los devuelva al default en
silencio — el modo de fallar es mudo (el server queda "OFFLINE para siempre" o
no despierta nunca) y no salta hasta la noche que no vuelve."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import config
import network
from schedule_store import _seeded_default


class WolTargetTests(unittest.TestCase):
    def test_local_server_uses_broadcast(self):
        with patch("network.subprocess.run") as run, \
             patch("network.os.path.exists", return_value=True):
            run.return_value.returncode = 0
            self.assertTrue(network.send_wol("AA:BB:CC:DD:EE:FF"))
        self.assertEqual(run.call_args[0][0], ["/usr/bin/wakeonlan", "AA:BB:CC:DD:EE:FF"])

    def test_remote_vlan_server_uses_unicast(self):
        """Sin -i el paquete sale a 255.255.255.255, que ningún router reenvía."""
        with patch("network.subprocess.run") as run, \
             patch("network.os.path.exists", return_value=True):
            run.return_value.returncode = 0
            self.assertTrue(network.send_wol("AA:BB:CC:DD:EE:FF", "192.168.1.70", 9))
        self.assertEqual(
            run.call_args[0][0],
            ["/usr/bin/wakeonlan", "-i", "192.168.1.70", "-p", "9", "AA:BB:CC:DD:EE:FF"],
        )

    def test_proxmox_wake_sends_unicast(self):
        with patch("network.send_wol", return_value=True) as send:
            asyncio.run(network.wake("proxmox"))
        send.assert_called_once_with(
            config.SERVERS["proxmox"]["mac"], "192.168.1.70", None
        )


class ProbeDispatchTests(unittest.TestCase):
    def test_proxmox_probes_tcp_not_icmp(self):
        """El gateway descarta ICMP entre VLANs: un ping daría siempre caído."""
        with patch("network.tcp_probe", new=AsyncMock(return_value=True)) as tcp, \
             patch("network.check_status", new=AsyncMock()) as icmp:
            self.assertTrue(asyncio.run(network.probe("proxmox")))
        tcp.assert_awaited_once_with("192.168.1.70", 22)
        icmp.assert_not_awaited()

    def test_local_servers_keep_icmp(self):
        with patch("network.check_status", new=AsyncMock(return_value=True)) as icmp, \
             patch("network.tcp_probe", new=AsyncMock()) as tcp:
            self.assertTrue(asyncio.run(network.probe("media")))
        icmp.assert_awaited_once_with(config.SERVERS["media"]["ip"])
        tcp.assert_not_awaited()

    def test_closed_port_reads_as_down(self):
        self.assertFalse(asyncio.run(network.tcp_probe("127.0.0.1", 1, timeout=1)))


class RemoteCommandTests(unittest.TestCase):
    def test_default_servers_use_sudo_shutdown(self):
        self.assertEqual(config.shutdown_cmd("nas"), "sudo shutdown -h now")
        self.assertEqual(config.reboot_cmd("media"), "sudo shutdown -r now")

    def test_proxmox_uses_forced_command_words(self):
        """Entra por clave de comando forzado: solo acepta halt/reboot."""
        self.assertEqual(config.shutdown_cmd("proxmox"), "halt")
        self.assertEqual(config.reboot_cmd("proxmox"), "reboot")

    def test_ssh_shutdown_sends_the_configured_command(self):
        with patch("network.ssh_run", new=AsyncMock(return_value=True)) as run:
            asyncio.run(network.ssh_shutdown("proxmox"))
        run.assert_awaited_once_with("proxmox", "halt")


class ScheduleSeedTests(unittest.TestCase):
    def test_proxmox_seeds_enabled_with_its_window(self):
        cfg = _seeded_default("proxmox")
        self.assertTrue(cfg["enabled"])
        self.assertEqual(cfg["wake_time"], "08:00")
        self.assertEqual(cfg["shutdown_time"], "23:00")

    def test_other_servers_stay_opt_in(self):
        self.assertFalse(_seeded_default("nas")["enabled"])

    def test_stored_value_beats_the_seed(self):
        """Pausar el horario desde /schedule tiene que ganarle a la semilla."""
        import json, tempfile, os
        from unittest.mock import patch as p
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "schedule.json")
            with open(path, "w") as f:
                json.dump({"proxmox": {"enabled": False}}, f)
            with p.object(config, "SCHEDULE_FILE", path):
                import schedule_store
                self.assertFalse(schedule_store.load_schedule("proxmox")["enabled"])


if __name__ == "__main__":
    unittest.main()
