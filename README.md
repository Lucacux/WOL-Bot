# 🔌 WOL-Bot

![WOL-Bot Banner](./assets/banner.png)

A Discord bot for Wake-on-LAN and remote power management of homelab servers, with an interactive control panel and automated wake/shutdown scheduling.

## ✨ Key Features

- **Interactive control panel (`/wol`):** buttons to wake, shut down, or check the real-time status (online/offline) of each configured server.
- **Remote shutdown & reboot via SSH (`/shutdown`, `/reboot`):** interactive panels to power off or restart any ONLINE server with a confirmation step and live progress monitoring; sends `sudo shutdown -h now` / `sudo shutdown -r now` over SSH.
- **Automated power scheduling (`/schedule`), per server:** each server gets its own configurable daily wake and shutdown times. The `/schedule` panel has a server selector, so NAS and media (and any server you add) are managed independently. Schedules are **opt-in** — disabled by default, so a fresh deploy never powers a server off by surprise.
- **Failsafe watchdog, per server:** if a server is down during the hours it should be online (its own `/schedule` window), the bot automatically re-sends WOL with debounced, low-impact ICMP checks and exponential-ish backoff — recovering from crashes or power outages with minimal downtime. Each server keeps its own failsafe state; toggleable from its `/schedule` panel.
- **Maintenance coordination:** `wolctl.py` lets local automation wake a server with bounded retries and acquire expiring maintenance leases. While a lease is active, scheduled shutdown is postponed instead of interrupting Ansible or another long-running job.
- **Power restore (`power-restore`):** automation that woke a server outside its uptime window can hand it back. The server is powered off again **only** if it is not inside its `/schedule` window and nobody else holds a maintenance lease — so a 3 AM backup does not leave a node running all day, and does not shut down a node the scheduler wanted online.
- **Multi-server support:** tracks multiple homelab nodes (NAS, media server, Proxmox hypervisor) with independent MAC/IP/SSH configuration per server. Every feature iterates over the server list — adding a node is a config entry, not a code change.
- **Cross-VLAN servers:** a node on a different VLAN than the bot gets two per-server overrides — `wol_target` (unicast magic packet, since limited broadcast is never routed) and `probe: "tcp"` (a TCP connect instead of ICMP, which inter-VLAN gateways typically drop). Remote shutdown/reboot commands are per-server too, so a node reachable only through a forced-command SSH key works like any other.

## 🧰 Stack

- Python
- discord.py
- `wakeonlan` (WOL magic packets)
- SSH (remote shutdown/reboot)

## 🗂️ Project structure

Each module has a single reason to change:

```
WOL-Bot/
├── config.py      # env, servidores (SERVERS) y constantes
├── network.py     # ping, WOL, ssh_run/reboot/shutdown (I/O de red, sin Discord)
├── embeds.py      # todos los build_*_embed
├── monitors.py    # monitores de boot/reboot/shutdown (editan un mensaje)
├── scheduler.py   # persistencia schedule.json, schedule_loop y failsafe_loop
├── maintenance.py # reservas IPC con TTL que bloquean apagados programados
├── schedule_store.py # persistencia del horario + aritmética de la franja (sin discord)
├── orchestrator.py# encendido con reintentos, espera de boot y power-restore
├── schedule_store.py # franja horaria + persistencia, sin depender de discord
├── wolctl.py       # CLI local consumida por Updates-Bot y homelab-backup
├── views.py       # todas las Views/Modals de discord.ui
└── main.py        # crear bot, registrar comandos, run
```

## 🚀 Installation

```bash
git clone https://github.com/Lucacux/WOL-Bot.git
cd WOL-Bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in your real values
python main.py
```

## ⚙️ Environment Variables

See `.env.example` — bot token, reporting channel, network interface, and per-server name/MAC/IP/SSH configuration.

## 🤝 Local automation contract

Updates-Bot calls this repository's CLI instead of duplicating MAC addresses or
Wake-on-LAN behavior:

```bash
./venv/bin/python wolctl.py maintenance-acquire nas --owner updates-bot-daily --ttl 10800
./venv/bin/python wolctl.py ensure-online nas --attempts 3 --attempt-timeout 180 --boot-grace 90 --json
./venv/bin/python wolctl.py maintenance-release nas --owner updates-bot-daily
```

Leases are local, owner-aware, and expire automatically. `maintenance.json` is
runtime state and is intentionally ignored by Git.

`homelab-backup`'s orchestrator adds one more step: it runs backups at 03:00,
when both the NAS and the media server are normally off, so after releasing the
lease it hands the node back.

```bash
./venv/bin/python wolctl.py schedule-window nas          # ¿debería estar encendido ahora?
./venv/bin/python wolctl.py power-restore nas --owner homelab-backup --json
```

`power-restore` refuses to power off a node that is inside its `/schedule`
window, one that is already offline, or one held by **another** owner's lease
(your own lease is ignored, so you can release it after the shutdown decision).
Exit code `7` means the shutdown was attempted and failed.

Only call it for nodes you woke: the CLI decides whether powering off is *safe*,
not whether it is *yours to do*.

## 🧱 Servidores en otra VLAN

El bot manda el magic packet por broadcast y sondea por ICMP, lo cual alcanza
mientras todos los servidores compartan segmento con él. Un nodo en otra VLAN
—el hipervisor Proxmox, en esta instalación— rompe los dos supuestos:

| Supuesto | Por qué se rompe | Override |
|---|---|---|
| El broadcast llega | `255.255.255.255` es *limited broadcast*: ningún router lo reenvía, nunca. No hay regla de firewall que lo arregle. | `wol_target` → el paquete sale unicast a esa IP y el router sí lo rutea. La NIC lo reconoce igual: el magic packet se detecta por patrón dentro de la trama. |
| El ping responde | El gateway descarta ICMP entre VLANs, así que el server figura caído siempre y el failsafe reenvía WOL en loop. | `probe: "tcp"` + `probe_port` → connect TCP. Además prueba algo más fuerte que el ping: que el servicio levantó. |

Ambos necesitan acompañamiento del lado de la red (una ARP permanente y dos
reglas de firewall) y del lado del servidor (usuario dedicado con clave de
comando forzado para el apagado). El procedimiento completo, con verificación
y rollback, está en **[`docs/RUNBOOK-proxmox.md`](docs/RUNBOOK-proxmox.md)**.

## 📄 License

Personal infrastructure project — free to use as reference.
