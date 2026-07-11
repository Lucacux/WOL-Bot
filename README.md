# 🔌 WOL-Bot

![WOL-Bot Banner](./assets/banner.png)

A Discord bot for Wake-on-LAN and remote power management of homelab servers, with an interactive control panel and automated wake/shutdown scheduling.

## ✨ Key Features

- **Interactive control panel (`/wol`):** buttons to wake, shut down, or check the real-time status (online/offline) of each configured server.
- **Remote shutdown & reboot via SSH (`/shutdown`, `/reboot`):** interactive panels to power off or restart any ONLINE server with a confirmation step and live progress monitoring; sends `sudo shutdown -h now` / `sudo shutdown -r now` over SSH.
- **Automated power scheduling (`/schedule`), per server:** each server gets its own configurable daily wake and shutdown times. The `/schedule` panel has a server selector, so NAS and media (and any server you add) are managed independently. Schedules are **opt-in** — disabled by default, so a fresh deploy never powers a server off by surprise.
- **Failsafe watchdog, per server:** if a server is down during the hours it should be online (its own `/schedule` window), the bot automatically re-sends WOL with debounced, low-impact ICMP checks and exponential-ish backoff — recovering from crashes or power outages with minimal downtime. Each server keeps its own failsafe state; toggleable from its `/schedule` panel.
- **Multi-server support:** tracks multiple homelab nodes (NAS, media server) with independent MAC/IP/SSH configuration per server. Every feature iterates over the server list — adding a node is a config entry, not a code change.

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

## 📄 License

Personal infrastructure project — free to use as reference.
