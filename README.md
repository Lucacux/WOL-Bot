# 🔌 WOL-Bot

![WOL-Bot Banner](./assets/banner.png)

A Discord bot for Wake-on-LAN and remote power management of homelab servers, with an interactive control panel and automated wake/shutdown scheduling.

## ✨ Key Features

- **Interactive control panel (`/wol`):** buttons to wake, shut down, or check the real-time status (online/offline) of each configured server.
- **Remote shutdown & reboot via SSH (`/shutdown`, `/reboot`):** interactive panels to power off or restart any ONLINE server with a confirmation step and live progress monitoring; sends `sudo shutdown -h now` / `sudo shutdown -r now` over SSH.
- **Automated power scheduling (`/schedule`):** configurable daily wake and shutdown times for the media server.
- **Failsafe watchdog:** if the media server is down during the hours it should be online (its `/schedule` window), the bot automatically re-sends WOL with debounced, low-impact ICMP checks and exponential-ish backoff — recovering from crashes or power outages with minimal downtime. Toggleable from the `/schedule` panel.
- **Multi-server support:** tracks multiple homelab nodes (NAS, media server) with independent MAC/IP/SSH configuration per server.

## 🧰 Stack

- Python
- discord.py
- `wakeonlan` (WOL magic packets)
- SSH (remote shutdown/reboot)

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
