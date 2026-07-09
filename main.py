import discord
import asyncio
import subprocess
import os
import sys
import json
import time as _time
from datetime import datetime, time as dtime, date, timedelta
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────
TOKEN         = os.getenv('DISCORD_TOKEN')
CHANNEL_ID    = int(os.getenv('DISCORD_CHANNEL_ID'))
WOL_INTERFACE = os.getenv('WOL_INTERFACE', 'eth0.20')

SSH_USER_MEDIA = os.getenv('SSH_USER_MEDIA', 'luca')
SSH_KEY_MEDIA  = os.getenv('SSH_KEY_MEDIA',  os.path.expanduser('~/.ssh/id_ed25519_wol'))
SSH_PORT_MEDIA = os.getenv('SSH_PORT_MEDIA', '2222')

# ── SSH config por servidor (shutdown / reboot) ──
SSH_CONFIG = {
    "nas": {
        "user": os.getenv('SSH_USER_NAS', SSH_USER_MEDIA),
        "key":  os.getenv('SSH_KEY_NAS',  SSH_KEY_MEDIA),
        "port": os.getenv('SSH_PORT_NAS', '22'),
    },
    "media": {
        "user": SSH_USER_MEDIA,
        "key":  SSH_KEY_MEDIA,
        "port": SSH_PORT_MEDIA,
    },
}

SERVERS = {
    "nas": {
        "name": os.getenv('NAME_NAS',   'NAS Fileserver'),
        "mac":  os.getenv('MAC_NAS',    '00:13:8F:98:6A:08'),
        "ip":   os.getenv('IP_NAS',     '192.168.2.20'),
    },
    "media": {
        "name": os.getenv('NAME_MEDIA', 'Homeserver Multimedia'),
        "mac":  os.getenv('MAC_MEDIA',  '84:2B:2B:7F:44:33'),
        "ip":   os.getenv('IP_MEDIA',   '192.168.2.10'),
    },
}

# ──────────────────────────────────────────
# SCHEDULE CONFIG
# ──────────────────────────────────────────
SCHEDULE_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.json")
SCHEDULE_CHECK_SECS   = 30    # frecuencia del loop de verificación
SHUTDOWN_WARN_SECS    = 120   # ventana de aviso antes del apagado (2 minutos)
SHUTDOWN_WARN_REFRESH = 10    # refresca el countdown cada N segundos

DEFAULT_SCHEDULE = {
    "enabled":                 True,
    "wake_time":               "06:30",
    "shutdown_time":           "23:00",
    "last_wake_date":          None,
    "last_shutdown_date":      None,
    "shutdown_cancelled_date": None,
    "failsafe_enabled":        True,   # watchdog WOL dentro de la franja activa
}

# ──────────────────────────────────────────
# FAILSAFE CONFIG (watchdog de encendido)
# ──────────────────────────────────────────
# Si el Homeserver Multimedia está caído dentro de su franja "debería-estar-
# encendido" (wake_time → shutdown_time), el failsafe reenvía WOL solo.
# Ping controlado: en estado normal es 1 paquete ICMP cada FAILSAFE_CHECK_SECS.
# Solo escala a varios pings cuando el primero falla, para confirmar la caída.
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default

FAILSAFE_CHECK_SECS     = _env_int('FAILSAFE_CHECK_SECS', 60)    # cadencia del watchdog
FAILSAFE_CONFIRM_CHECKS = _env_int('FAILSAFE_CONFIRM_CHECKS', 3) # pings consecutivos fallidos = caída
FAILSAFE_CONFIRM_GAP    = _env_int('FAILSAFE_CONFIRM_GAP', 5)    # segundos entre pings de confirmación
FAILSAFE_WOL_COOLDOWN   = _env_int('FAILSAFE_WOL_COOLDOWN', 180) # espera tras un WOL (deja bootear)
FAILSAFE_MAX_FAST_TRIES = _env_int('FAILSAFE_MAX_FAST_TRIES', 3) # intentos antes de pasar a modo lento
FAILSAFE_SLOW_COOLDOWN  = _env_int('FAILSAFE_SLOW_COOLDOWN', 600)# cooldown en modo lento (ej. apagón)
FAILSAFE_BOOT_GRACE     = _env_int('FAILSAFE_BOOT_GRACE', 150)   # gracia al entrar en franja / reinicio

# ──────────────────────────────────────────
# SCHEDULE — persistencia
# ──────────────────────────────────────────
def load_schedule() -> dict:
    if os.path.exists(SCHEDULE_FILE):
        try:
            with open(SCHEDULE_FILE, encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_SCHEDULE.items():
                data.setdefault(k, v)
            return data
        except Exception as e:
            print(f"[schedule] Error leyendo {SCHEDULE_FILE}: {e}")
    return DEFAULT_SCHEDULE.copy()

def save_schedule(cfg: dict):
    with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

def parse_hhmm(t: str) -> dtime:
    parts = t.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Formato inválido: {t!r}")
    return dtime(int(parts[0]), int(parts[1]))

# ──────────────────────────────────────────
# HELPERS — ping / WOL / SSH shutdown
# ──────────────────────────────────────────
def ping(ip: str) -> bool:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def send_wol(mac: str) -> bool:
    wol_bin = "/usr/bin/wakeonlan"
    if not os.path.exists(wol_bin):
        import shutil
        wol_bin = shutil.which("wakeonlan")
        if not wol_bin:
            return False
    r = subprocess.run(
        [wol_bin, mac],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0

async def check_status(ip: str) -> bool:
    return await asyncio.to_thread(ping, ip)

def in_uptime_window(now_t: dtime, wake_t: dtime, shut_t: dtime) -> bool:
    """¿Está `now_t` dentro de la franja en la que el server debería estar ON?

    Ventana [wake_t, shut_t). Si shut_t <= wake_t la franja cruza medianoche
    (coherente con la lógica de apagado). shut_t == wake_t ⇒ 24h (siempre ON).
    """
    if shut_t == wake_t:
        return True
    if wake_t < shut_t:
        return wake_t <= now_t < shut_t
    return now_t >= wake_t or now_t < shut_t

async def is_media_down() -> bool:
    """Detección de caída con debounce del Homeserver Multimedia.

    Devuelve True solo tras FAILSAFE_CONFIRM_CHECKS pings consecutivos fallidos.
    En estado normal (server ONLINE) el primer ping responde y sale con 1 solo
    paquete → impacto de red despreciable. Solo cuando está caído escala a
    varios pings espaciados para confirmar y evitar falsos positivos.
    """
    ip = SERVERS["media"]["ip"]
    for i in range(FAILSAFE_CONFIRM_CHECKS):
        if await check_status(ip):
            return False
        if i < FAILSAFE_CONFIRM_CHECKS - 1:
            await asyncio.sleep(FAILSAFE_CONFIRM_GAP)
    return True

async def ssh_shutdown_media() -> bool:
    cmd = [
        "ssh",
        "-i", SSH_KEY_MEDIA,
        "-p", SSH_PORT_MEDIA,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{SSH_USER_MEDIA}@{SERVERS['media']['ip']}",
        "sudo shutdown -h now",
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0

async def ssh_run(server_key: str, remote_cmd: str) -> bool:
    srv = SERVERS[server_key]
    sc  = SSH_CONFIG[server_key]
    cmd = [
        "ssh",
        "-i", sc["key"],
        "-p", sc["port"],
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{sc['user']}@{srv['ip']}",
        remote_cmd,
    ]
    result = await asyncio.to_thread(
        subprocess.run, cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    return result.returncode == 0

async def ssh_reboot(server_key: str) -> bool:
    # 'shutdown -r now' en vez de 'reboot' para reutilizar el mismo
    # NOPASSWD de sudo que ya tenés configurado para el apagado.
    return await ssh_run(server_key, "sudo shutdown -r now")

async def ssh_shutdown(server_key: str) -> bool:
    # Apagado genérico por servidor (mismo NOPASSWD de sudo que el reboot).
    # ssh_shutdown_media() sigue existiendo aparte para el apagado programado.
    return await ssh_run(server_key, "sudo shutdown -h now")

def status_line(online: bool) -> str:
    return "🟢 ONLINE" if online else "🔴 OFFLINE"

# ──────────────────────────────────────────
# EMBED BUILDERS
# ──────────────────────────────────────────
def build_panel_embed(nas_online: bool, media_online: bool) -> discord.Embed:
    embed = discord.Embed(
        title="🖥️  WOL Control Panel",
        color=0x2ecc71 if (nas_online and media_online) else 0xe67e22,
        timestamp=datetime.now(),
    )
    embed.add_field(
        name=f"🗄️  {SERVERS['nas']['name']}",
        value=f"{status_line(nas_online)}\n`{SERVERS['nas']['ip']}`",
        inline=True,
    )
    embed.add_field(
        name=f"📺  {SERVERS['media']['name']}",
        value=f"{status_line(media_online)}\n`{SERVERS['media']['ip']}`",
        inline=True,
    )
    embed.set_footer(text="Usá los botones para despertar un servidor")
    return embed

def build_schedule_embed(cfg: dict) -> discord.Embed:
    enabled = cfg["enabled"]
    embed = discord.Embed(
        title="📅  Horario Automático — Multimedia",
        description=f"Estado: {'🟢 **Activo**' if enabled else '⏸️  **Pausado**'}",
        color=0x2ecc71 if enabled else 0x95a5a6,
        timestamp=datetime.now(),
    )
    embed.add_field(name="⏰  Encendido",  value=f"`{cfg['wake_time']}`",     inline=True)
    embed.add_field(name="🌙  Apagado",    value=f"`{cfg['shutdown_time']}`", inline=True)
    embed.add_field(name="\u200b",         value="\u200b",                    inline=True)

    today = date.today().isoformat()
    notes = []
    if cfg.get("last_wake_date") == today:
        notes.append("✅ WOL enviado hoy")
    if cfg.get("last_shutdown_date") == today:
        if cfg.get("shutdown_cancelled_date") == today:
            notes.append("⚠️  Apagado cancelado manualmente — se retoma mañana")
        else:
            notes.append("✅ Apagado ejecutado hoy")
    if not notes:
        notes.append("Sin acciones ejecutadas hoy")

    embed.add_field(name="📋  Actividad de hoy", value="\n".join(notes), inline=False)

    fs = cfg.get("failsafe_enabled", True)
    embed.add_field(
        name="🛟  Failsafe (watchdog WOL)",
        value=(
            f"{'🟢 **Activo**' if fs else '⏸️  **Pausado**'} — reenciende solo el "
            f"servidor si se cae dentro de la franja `{cfg['wake_time']}`–`{cfg['shutdown_time']}`."
        ),
        inline=False,
    )
    embed.set_footer(text="Solo aplica al Homeserver Multimedia")
    return embed

def build_shutdown_warning_embed(shutdown_time: str, remaining: float) -> discord.Embed:
    mins   = int(remaining // 60)
    secs   = int(remaining % 60)
    filled = max(0, min(16, int((1.0 - remaining / SHUTDOWN_WARN_SECS) * 16)))
    bar    = "█" * filled + "░" * (16 - filled)

    embed = discord.Embed(
        title=f"⚠️  Apagado automático en {mins:02d}:{secs:02d}",
        description=(
            f"**{SERVERS['media']['name']}** se apaga a las `{shutdown_time}` según el horario programado.\n"
            f"Presioná **No apagar** para cancelar por esta noche."
        ),
        color=0xe67e22,
        timestamp=datetime.now(),
    )
    embed.add_field(name="⏱️  ETA", value=f"`{bar}` **{mins:02d}:{secs:02d}**", inline=False)
    embed.set_footer(text="Sin respuesta → apagado automático")
    return embed

# ──────────────────────────────────────────
# VIEW — botón cancelar apagado
# ──────────────────────────────────────────
class CancelShutdownView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=SHUTDOWN_WARN_SECS + 15)
        self.cancelled = False
        btn = discord.ui.Button(
            label="🚫  No apagar",
            style=discord.ButtonStyle.danger,
        )
        btn.callback = self._cancel
        self.add_item(btn)

    async def _cancel(self, interaction: discord.Interaction):
        self.cancelled = True
        self.stop()
        await interaction.response.defer()

# ──────────────────────────────────────────
# SCHEDULE — aviso con countdown + apagado
# ──────────────────────────────────────────
async def run_shutdown_warning(cfg: dict, today: str):
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    view  = CancelShutdownView()
    embed = build_shutdown_warning_embed(cfg["shutdown_time"], SHUTDOWN_WARN_SECS)
    msg   = await channel.send(embed=embed, view=view)

    start = datetime.now()

    while True:
        elapsed   = (datetime.now() - start).total_seconds()
        remaining = max(0.0, SHUTDOWN_WARN_SECS - elapsed)

        if view.cancelled:
            cfg = load_schedule()
            cfg["shutdown_cancelled_date"] = today
            save_schedule(cfg)
            await msg.edit(
                embed=discord.Embed(
                    title="✅  Apagado cancelado por esta noche",
                    description=(
                        f"**{SERVERS['media']['name']}** seguirá encendido.\n"
                        f"El horario automático se retoma mañana."
                    ),
                    color=0x2ecc71,
                    timestamp=datetime.now(),
                ),
                view=None,
            )
            return

        if remaining <= 0:
            break

        await msg.edit(
            embed=build_shutdown_warning_embed(cfg["shutdown_time"], remaining),
            view=view,
        )
        await asyncio.sleep(SHUTDOWN_WARN_REFRESH)

    # Tiempo agotado → apagar
    await msg.edit(
        embed=discord.Embed(
            title=f"🔌  Apagando {SERVERS['media']['name']}...",
            description="Enviando comando de apagado vía SSH...",
            color=0xe74c3c,
            timestamp=datetime.now(),
        ),
        view=None,
    )

    success = await ssh_shutdown_media()

    await msg.edit(
        embed=discord.Embed(
            title="🔴  Servidor apagado" if success else "❌  Error al apagar",
            description=(
                f"**{SERVERS['media']['name']}** fue apagado automáticamente."
                if success else
                "No se pudo conectar por SSH. Verificá la clave y el permiso de sudo."
            ),
            color=0x992d22 if success else 0xff0000,
            timestamp=datetime.now(),
        )
    )

# ──────────────────────────────────────────
# SCHEDULE LOOP — background task
# ──────────────────────────────────────────
async def schedule_loop():
    await bot.wait_until_ready()
    print("[schedule] Loop iniciado.")

    while not bot.is_closed():
        try:
            cfg = load_schedule()

            if cfg["enabled"]:
                now   = datetime.now()
                today = now.date().isoformat()

                wake_t = parse_hhmm(cfg["wake_time"])
                shut_t = parse_hhmm(cfg["shutdown_time"])

                # ── ENCENDIDO ──
                if now.time() >= wake_t and cfg.get("last_wake_date") != today:
                    cfg["last_wake_date"] = today
                    save_schedule(cfg)

                    online = await check_status(SERVERS["media"]["ip"])
                    if not online:
                        wol_ok  = await asyncio.to_thread(send_wol, SERVERS["media"]["mac"])
                        channel = bot.get_channel(CHANNEL_ID)
                        if channel:
                            if wol_ok:
                                # Notificación + monitor con ETA igual que el encendido manual
                                notify_embed = discord.Embed(
                                    title="⏰  Encendido automático",
                                    description=(
                                        f"Magic packet enviado a **{SERVERS['media']['name']}**.\n"
                                        f"Horario configurado: `{cfg['wake_time']}`"
                                    ),
                                    color=0x3498db,
                                    timestamp=datetime.now(),
                                )
                                await channel.send(embed=notify_embed)
                                monitor_embed = discord.Embed(
                                    title=f"⏳  Iniciando: {SERVERS['media']['name']}",
                                    color=0x3498db,
                                    timestamp=datetime.now(),
                                )
                                monitor_msg = await channel.send(embed=monitor_embed)
                                asyncio.create_task(monitor_boot(monitor_msg, "media"))
                            else:
                                await channel.send(
                                    embed=discord.Embed(
                                        title="❌  Error en encendido automático",
                                        description=(
                                            f"No se pudo enviar el magic packet a **{SERVERS['media']['name']}**.\n"
                                            f"Verificá que `wakeonlan` esté instalado."
                                        ),
                                        color=0xe74c3c,
                                        timestamp=datetime.now(),
                                    )
                                )

                # ── APAGADO ──
                # El apagado se ancla a la FECHA real del último encendido, no a
                # "hoy a las shut_t". Si shutdown_time <= wake_time la ventana cruza
                # medianoche (ej: enciende 12:00, apaga 01:00 del día siguiente) y el
                # apagado cae al día siguiente. Comparar solo la hora del día hacía
                # que 'now.time() >= warn_t' fuera verdadero TODO el día en ese caso,
                # disparando el apagado apenas se guardaba el horario.
                last_wake = cfg.get("last_wake_date")
                if last_wake:
                    shutdown_dt = datetime.combine(date.fromisoformat(last_wake), shut_t)
                    if shut_t <= wake_t:
                        shutdown_dt += timedelta(days=1)
                    warn_dt = shutdown_dt - timedelta(seconds=SHUTDOWN_WARN_SECS)

                    if (
                        now >= warn_dt
                        and cfg.get("last_shutdown_date") != today
                        and cfg.get("shutdown_cancelled_date") != today
                    ):
                        cfg["last_shutdown_date"] = today
                        save_schedule(cfg)
                        asyncio.create_task(run_shutdown_warning(cfg, today))

        except Exception as e:
            print(f"[schedule_loop] Excepción: {e}")

        await asyncio.sleep(SCHEDULE_CHECK_SECS)

# ──────────────────────────────────────────
# FAILSAFE — watchdog de encendido (background task)
# ──────────────────────────────────────────
# Vigila que el Homeserver Multimedia esté ONLINE dentro de su franja activa.
# Si lo encuentra caído, reenvía WOL con backoff para minimizar downtime sin
# saturar la red ni el canal de Discord.
_failsafe = {
    "active":        False,   # hay una caída en curso siendo atendida
    "outage_start":  None,    # _time.monotonic() del inicio de la caída
    "last_wol":      None,    # _time.monotonic() del último WOL enviado
    "attempts":      0,       # WOLs enviados en esta caída
    "window_since":  None,    # _time.monotonic() en que entramos a la franja
}

def _reset_failsafe(keep_window: bool = False):
    win = _failsafe["window_since"] if keep_window else None
    _failsafe.update(active=False, outage_start=None, last_wol=None,
                     attempts=0, window_since=win)

def build_failsafe_alert_embed(attempt: int, wol_ok: bool, slow_mode: bool) -> discord.Embed:
    srv = SERVERS["media"]
    if not wol_ok:
        return discord.Embed(
            title="❌  Failsafe: no se pudo enviar WOL",
            description=(
                f"**{srv['name']}** está caído en su franja activa, pero falló el "
                f"magic packet. Verificá que `wakeonlan` esté instalado."
            ),
            color=0xff0000,
            timestamp=datetime.now(),
        )
    embed = discord.Embed(
        title="🛟  Failsafe activado — reenviando WOL",
        description=(
            f"**{srv['name']}** (`{srv['ip']}`) debería estar encendido y no "
            f"responde. Se envió un magic packet automático."
        ),
        color=0xe67e22 if not slow_mode else 0xf1c40f,
        timestamp=datetime.now(),
    )
    embed.add_field(name="MAC",     value=f"`{srv['mac']}`", inline=True)
    embed.add_field(name="Intento", value=f"`#{attempt}`",  inline=True)
    if slow_mode:
        embed.add_field(
            name="Modo",
            value=f"`lento` — reintenta cada {FAILSAFE_SLOW_COOLDOWN // 60} min",
            inline=True,
        )
        embed.set_footer(text="Sin respuesta tras varios intentos (¿apagón?). Seguirá vigilando.")
    else:
        embed.set_footer(text="Monitoreando la recuperación...")
    return embed

def build_failsafe_recovered_embed(downtime_secs: float, attempts: int) -> discord.Embed:
    mins = int(downtime_secs // 60)
    secs = int(downtime_secs % 60)
    return discord.Embed(
        title=f"✅  Failsafe: {SERVERS['media']['name']} recuperado",
        description=(
            f"El servidor volvió a estar ONLINE tras `{mins:02d}:{secs:02d}` "
            f"de caída y **{attempts}** intento(s) de WOL."
        ),
        color=0x2ecc71,
        timestamp=datetime.now(),
    )

async def failsafe_loop():
    await bot.wait_until_ready()
    print("[failsafe] Loop iniciado.")

    while not bot.is_closed():
        try:
            cfg = load_schedule()

            active_window = bool(
                cfg.get("enabled")
                and cfg.get("failsafe_enabled", True)
                and in_uptime_window(
                    datetime.now().time(),
                    parse_hhmm(cfg["wake_time"]),
                    parse_hhmm(cfg["shutdown_time"]),
                )
            )

            if not active_window:
                # Fuera de franja (o failsafe/schedule pausado): no vigilamos.
                if _failsafe["active"] or _failsafe["window_since"] is not None:
                    _reset_failsafe()
                await asyncio.sleep(FAILSAFE_CHECK_SECS)
                continue

            # Marca de entrada a la franja (para la gracia de arranque).
            if _failsafe["window_since"] is None:
                _failsafe["window_since"] = _time.monotonic()

            down = await is_media_down()

            if not down:
                # ONLINE. Si veníamos de una caída atendida → avisar recuperación.
                if _failsafe["active"]:
                    channel = bot.get_channel(CHANNEL_ID)
                    if channel:
                        downtime = _time.monotonic() - (_failsafe["outage_start"] or _time.monotonic())
                        await channel.send(embed=build_failsafe_recovered_embed(
                            downtime, _failsafe["attempts"],
                        ))
                _reset_failsafe(keep_window=True)
                await asyncio.sleep(FAILSAFE_CHECK_SECS)
                continue

            # ── CAÍDA CONFIRMADA dentro de la franja ──
            now_m = _time.monotonic()

            # Gracia al recién entrar en franja (o tras reiniciar el bot): dejamos
            # que el encendido programado (schedule_loop) y el boot ocurran sin
            # duplicar el WOL ni gritar una falsa alarma.
            if now_m - _failsafe["window_since"] < FAILSAFE_BOOT_GRACE:
                await asyncio.sleep(FAILSAFE_CHECK_SECS)
                continue

            if not _failsafe["active"]:
                _failsafe["active"]       = True
                _failsafe["outage_start"] = now_m

            slow_mode = _failsafe["attempts"] >= FAILSAFE_MAX_FAST_TRIES
            cooldown  = FAILSAFE_SLOW_COOLDOWN if slow_mode else FAILSAFE_WOL_COOLDOWN
            due = _failsafe["last_wol"] is None or (now_m - _failsafe["last_wol"]) >= cooldown

            if due:
                wol_ok = await asyncio.to_thread(send_wol, SERVERS["media"]["mac"])
                _failsafe["last_wol"] = now_m
                _failsafe["attempts"] += 1
                channel = bot.get_channel(CHANNEL_ID)
                if channel:
                    await channel.send(embed=build_failsafe_alert_embed(
                        _failsafe["attempts"], wol_ok, slow_mode,
                    ))

        except Exception as e:
            print(f"[failsafe_loop] Excepción: {e}")

        await asyncio.sleep(FAILSAFE_CHECK_SECS)

# ──────────────────────────────────────────
# MODALS — editar horas
# ──────────────────────────────────────────
class TimeInputModal(discord.ui.Modal):
    def __init__(self, field: str, current_val: str):
        label = "encendido" if field == "wake_time" else "apagado"
        super().__init__(title=f"Cambiar hora de {label}")
        self.field = field
        self.time_input = discord.ui.TextInput(
            label="Hora (formato HH:MM)",
            placeholder="Ej: 06:30",
            default=current_val,
            max_length=5,
            min_length=4,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.time_input.value.strip()
        try:
            parse_hhmm(val)
        except Exception:
            await interaction.response.send_message(
                "❌ Formato inválido. Usá `HH:MM` — por ejemplo `06:30`.",
                ephemeral=True,
            )
            return

        cfg = load_schedule()
        cfg[self.field] = val
        if self.field == "wake_time":
            cfg["last_wake_date"] = None
        else:
            cfg["last_shutdown_date"]      = None
            cfg["shutdown_cancelled_date"] = None
        save_schedule(cfg)

        label = "encendido" if self.field == "wake_time" else "apagado"
        await interaction.response.send_message(
            f"✅ Hora de **{label}** actualizada a `{val}`.", ephemeral=True
        )

# ──────────────────────────────────────────
# VIEW — Panel de horario
# ──────────────────────────────────────────
class SchedulePanel(discord.ui.View):
    def __init__(self, cfg: dict):
        super().__init__(timeout=300)
        self.cfg = cfg
        self._build_buttons()

    def _build_buttons(self):
        self.clear_items()
        enabled = self.cfg["enabled"]

        toggle = discord.ui.Button(
            label="⏸️  Pausar horario" if enabled else "▶️  Activar horario",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            row=0,
        )
        toggle.callback = self._toggle
        self.add_item(toggle)

        btn_wake = discord.ui.Button(
            label="⏰  Cambiar encendido",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        btn_wake.callback = self._set_wake
        self.add_item(btn_wake)

        btn_shut = discord.ui.Button(
            label="🌙  Cambiar apagado",
            style=discord.ButtonStyle.primary,
            row=1,
        )
        btn_shut.callback = self._set_shutdown
        self.add_item(btn_shut)

        fs_enabled = self.cfg.get("failsafe_enabled", True)
        btn_failsafe = discord.ui.Button(
            label="🛟  Pausar failsafe" if fs_enabled else "🛟  Activar failsafe",
            style=discord.ButtonStyle.danger if fs_enabled else discord.ButtonStyle.success,
            row=2,
        )
        btn_failsafe.callback = self._toggle_failsafe
        self.add_item(btn_failsafe)

        btn_reset = discord.ui.Button(
            label="🔄  Resetear estado de hoy",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        btn_reset.callback = self._reset_today
        self.add_item(btn_reset)

    async def _toggle(self, interaction: discord.Interaction):
        cfg = load_schedule()
        cfg["enabled"] = not cfg["enabled"]
        save_schedule(cfg)
        self.cfg = cfg
        self._build_buttons()
        await interaction.response.edit_message(embed=build_schedule_embed(cfg), view=self)

    async def _toggle_failsafe(self, interaction: discord.Interaction):
        cfg = load_schedule()
        cfg["failsafe_enabled"] = not cfg.get("failsafe_enabled", True)
        save_schedule(cfg)
        self.cfg = cfg
        self._build_buttons()
        await interaction.response.edit_message(embed=build_schedule_embed(cfg), view=self)

    async def _set_wake(self, interaction: discord.Interaction):
        cfg = load_schedule()
        await interaction.response.send_modal(TimeInputModal("wake_time", cfg["wake_time"]))

    async def _set_shutdown(self, interaction: discord.Interaction):
        cfg = load_schedule()
        await interaction.response.send_modal(TimeInputModal("shutdown_time", cfg["shutdown_time"]))

    async def _reset_today(self, interaction: discord.Interaction):
        cfg = load_schedule()
        cfg["last_wake_date"]          = None
        cfg["last_shutdown_date"]      = None
        cfg["shutdown_cancelled_date"] = None
        save_schedule(cfg)
        self.cfg = cfg
        await interaction.response.edit_message(embed=build_schedule_embed(cfg), view=self)

# ──────────────────────────────────────────
# BOOT MONITOR
# ──────────────────────────────────────────
async def monitor_boot(message: discord.Message, server_key: str):
    srv     = SERVERS[server_key]
    name    = srv["name"]
    ip      = srv["ip"]
    timeout = 120
    start   = datetime.now()
    attempt = 0

    while True:
        elapsed = int((datetime.now() - start).total_seconds())
        online  = await check_status(ip)
        attempt += 1

        filled = min(12, attempt // 3)
        bar    = "█" * filled + "░" * (12 - filled)

        embed = discord.Embed(
            title=f"⏳  Iniciando: {name}",
            color=0x3498db,
            timestamp=datetime.now(),
        )
        embed.add_field(name="IP",       value=f"`{ip}`",           inline=True)
        embed.add_field(name="Tiempo",   value=f"`{elapsed}s`",     inline=True)
        embed.add_field(name="Estado",   value=status_line(online), inline=True)
        embed.add_field(name="Progreso", value=f"`{bar}`",          inline=False)

        if online:
            embed.title       = f"✅  {name} despertó"
            embed.color       = 0x2ecc71
            embed.description = f"Servidor disponible en `{elapsed}s`."
            embed.remove_field(3)
            await message.edit(embed=embed, view=None)
            break

        if elapsed >= timeout:
            embed.title       = f"❌  {name} no respondió"
            embed.color       = 0xff0000
            embed.description = f"Sin respuesta después de `{timeout}s`.\nVerificá la conexión o el magic packet."
            await message.edit(embed=embed, view=None)
            break

        await message.edit(embed=embed)
        await asyncio.sleep(3)

# ──────────────────────────────────────────
# REBOOT MONITOR — dos fases (cae → vuelve)
# ──────────────────────────────────────────
async def monitor_reboot(message: discord.Message, server_key: str):
    srv   = SERVERS[server_key]
    name  = srv["name"]
    ip    = srv["ip"]
    start = datetime.now()

    DOWN_TIMEOUT = 90    # espera a que caiga
    UP_TIMEOUT   = 180   # espera a que vuelva

    # ── FASE 1: esperar que se apague ──
    went_down = False
    while (datetime.now() - start).total_seconds() < DOWN_TIMEOUT:
        if not await check_status(ip):
            went_down = True
            break
        embed = discord.Embed(
            title=f"🔄  Reiniciando: {name}",
            description="Esperando que el servidor se apague...",
            color=0xe67e22,
            timestamp=datetime.now(),
        )
        embed.add_field(name="IP",   value=f"`{ip}`",          inline=True)
        embed.add_field(name="Fase", value="`1/2 — apagando`", inline=True)
        await message.edit(embed=embed)
        await asyncio.sleep(3)

    if not went_down:
        await message.edit(embed=discord.Embed(
            title=f"⚠️  {name} no llegó a reiniciarse",
            description="El servidor nunca se desconectó. Verificá el comando o el permiso de sudo.",
            color=0xff0000,
            timestamp=datetime.now(),
        ))
        return

    # ── FASE 2: esperar que vuelva ──
    phase2  = datetime.now()
    attempt = 0
    while (datetime.now() - phase2).total_seconds() < UP_TIMEOUT:
        attempt += 1
        elapsed = int((datetime.now() - start).total_seconds())
        online  = await check_status(ip)
        filled  = min(12, attempt // 2)
        bar     = "█" * filled + "░" * (12 - filled)

        embed = discord.Embed(
            title=f"🔄  Reiniciando: {name}",
            color=0x3498db,
            timestamp=datetime.now(),
        )
        embed.add_field(name="IP",       value=f"`{ip}`",           inline=True)
        embed.add_field(name="Tiempo",   value=f"`{elapsed}s`",     inline=True)
        embed.add_field(name="Fase",     value="`2/2 — volviendo`", inline=True)
        embed.add_field(name="Progreso", value=f"`{bar}`",          inline=False)

        if online:
            embed.title       = f"✅  {name} reinició OK"
            embed.color       = 0x2ecc71
            embed.description = f"Volvió a estar disponible en `{elapsed}s`."
            embed.remove_field(3)
            await message.edit(embed=embed)
            return

        await message.edit(embed=embed)
        await asyncio.sleep(3)

    await message.edit(embed=discord.Embed(
        title=f"❌  {name} no volvió",
        description=f"Cayó pero no respondió en `{UP_TIMEOUT}s`. Verificá manualmente.",
        color=0xff0000,
        timestamp=datetime.now(),
    ))

# ──────────────────────────────────────────
# SHUTDOWN MONITOR — una fase (espera que caiga)
# ──────────────────────────────────────────
async def monitor_shutdown(message: discord.Message, server_key: str):
    srv   = SERVERS[server_key]
    name  = srv["name"]
    ip    = srv["ip"]
    start = datetime.now()

    DOWN_TIMEOUT = 120   # espera a que se apague

    attempt = 0
    while (datetime.now() - start).total_seconds() < DOWN_TIMEOUT:
        attempt += 1
        elapsed = int((datetime.now() - start).total_seconds())
        online  = await check_status(ip)
        filled  = min(12, attempt // 2)
        bar     = "█" * filled + "░" * (12 - filled)

        if not online:
            await message.edit(embed=discord.Embed(
                title=f"🔴  {name} apagado",
                description=f"El servidor dejó de responder tras `{elapsed}s`.",
                color=0x992d22,
                timestamp=datetime.now(),
            ))
            return

        embed = discord.Embed(
            title=f"🌙  Apagando: {name}",
            description="Esperando que el servidor se desconecte...",
            color=0xe67e22,
            timestamp=datetime.now(),
        )
        embed.add_field(name="IP",       value=f"`{ip}`",       inline=True)
        embed.add_field(name="Tiempo",   value=f"`{elapsed}s`", inline=True)
        embed.add_field(name="Progreso", value=f"`{bar}`",      inline=False)
        await message.edit(embed=embed)
        await asyncio.sleep(3)

    await message.edit(embed=discord.Embed(
        title=f"⚠️  {name} no se apagó",
        description=f"Sigue respondiendo después de `{DOWN_TIMEOUT}s`. Verificá manualmente.",
        color=0xff0000,
        timestamp=datetime.now(),
    ))

# ──────────────────────────────────────────
# VIEW — Confirmación de reinicio
# ──────────────────────────────────────────
class RebootConfirmView(discord.ui.View):
    def __init__(self, server_key: str):
        super().__init__(timeout=30)
        self.server_key = server_key

        confirm = discord.ui.Button(
            label="✅  Confirmar reinicio",
            style=discord.ButtonStyle.danger,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction):
        srv = SERVERS[self.server_key]

        online = await check_status(srv["ip"])
        if not online:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"ℹ️  {srv['name']} está OFFLINE",
                    description="No se puede reiniciar un servidor apagado. Usá el panel WOL para despertarlo.",
                    color=0x95a5a6,
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🔄  Enviando reinicio → {srv['name']}",
                description="Comando `sudo shutdown -r now` vía SSH...",
                color=0xe67e22,
                timestamp=datetime.now(),
            ),
            view=None,
        )

        ok = await ssh_reboot(self.server_key)
        if not ok:
            await interaction.edit_original_response(embed=discord.Embed(
                title="❌  Error al reiniciar",
                description="No se pudo conectar por SSH. Verificá clave, puerto y permiso de sudo.",
                color=0xff0000,
                timestamp=datetime.now(),
            ))
            return

        await interaction.edit_original_response(embed=discord.Embed(
            title=f"📡  Reinicio enviado → {srv['name']}",
            description="Monitoreando el ciclo de reinicio abajo 👇",
            color=0x2ecc71,
            timestamp=datetime.now(),
        ))

        monitor_msg = await interaction.channel.send(embed=discord.Embed(
            title=f"🔄  Reiniciando: {srv['name']}",
            color=0xe67e22,
            timestamp=datetime.now(),
        ))
        asyncio.create_task(monitor_reboot(monitor_msg, self.server_key))

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(title="✖️  Reinicio cancelado", color=0x95a5a6),
            view=None,
        )

# ──────────────────────────────────────────
# VIEW — Panel de reinicio
# ──────────────────────────────────────────
class RebootPanel(discord.ui.View):
    def __init__(self, nas_online: bool, media_online: bool):
        super().__init__(timeout=300)
        self.nas_online   = nas_online
        self.media_online = media_online
        self._build()

    def _build(self):
        self.clear_items()

        nas_btn = discord.ui.Button(
            label=f"Reiniciar {SERVERS['nas']['name']}" if self.nas_online
                  else f"{SERVERS['nas']['name']} OFFLINE",
            style=discord.ButtonStyle.danger if self.nas_online else discord.ButtonStyle.secondary,
            emoji="🗄️",
            disabled=not self.nas_online,
        )
        nas_btn.callback = self._reboot_nas
        self.add_item(nas_btn)

        media_btn = discord.ui.Button(
            label=f"Reiniciar {SERVERS['media']['name']}" if self.media_online
                  else f"{SERVERS['media']['name']} OFFLINE",
            style=discord.ButtonStyle.danger if self.media_online else discord.ButtonStyle.secondary,
            emoji="📺",
            disabled=not self.media_online,
        )
        media_btn.callback = self._reboot_media
        self.add_item(media_btn)

    async def _ask(self, interaction: discord.Interaction, server_key: str):
        srv = SERVERS[server_key]
        embed = discord.Embed(
            title=f"⚠️  ¿Reiniciar {srv['name']}?",
            description=(
                f"Vas a enviar `sudo shutdown -r now` a **{srv['name']}** (`{srv['ip']}`).\n"
                f"Se van a cortar los servicios mientras reinicia."
            ),
            color=0xe67e22,
        )
        await interaction.response.send_message(
            embed=embed, view=RebootConfirmView(server_key), ephemeral=True
        )

    async def _reboot_nas(self, interaction: discord.Interaction):
        await self._ask(interaction, "nas")

    async def _reboot_media(self, interaction: discord.Interaction):
        await self._ask(interaction, "media")

# ──────────────────────────────────────────
# VIEW — Confirmación de apagado
# ──────────────────────────────────────────
class ShutdownConfirmView(discord.ui.View):
    def __init__(self, server_key: str):
        super().__init__(timeout=30)
        self.server_key = server_key

        confirm = discord.ui.Button(
            label="✅  Confirmar apagado",
            style=discord.ButtonStyle.danger,
        )
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(
            label="Cancelar",
            style=discord.ButtonStyle.secondary,
        )
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction):
        srv = SERVERS[self.server_key]

        online = await check_status(srv["ip"])
        if not online:
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"ℹ️  {srv['name']} ya está OFFLINE",
                    description="El servidor ya está apagado.",
                    color=0x95a5a6,
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🌙  Enviando apagado → {srv['name']}",
                description="Comando `sudo shutdown -h now` vía SSH...",
                color=0xe67e22,
                timestamp=datetime.now(),
            ),
            view=None,
        )

        ok = await ssh_shutdown(self.server_key)
        if not ok:
            await interaction.edit_original_response(embed=discord.Embed(
                title="❌  Error al apagar",
                description="No se pudo conectar por SSH. Verificá clave, puerto y permiso de sudo.",
                color=0xff0000,
                timestamp=datetime.now(),
            ))
            return

        await interaction.edit_original_response(embed=discord.Embed(
            title=f"📡  Apagado enviado → {srv['name']}",
            description="Monitoreando la desconexión abajo 👇",
            color=0x2ecc71,
            timestamp=datetime.now(),
        ))

        monitor_msg = await interaction.channel.send(embed=discord.Embed(
            title=f"🌙  Apagando: {srv['name']}",
            color=0xe67e22,
            timestamp=datetime.now(),
        ))
        asyncio.create_task(monitor_shutdown(monitor_msg, self.server_key))

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(title="✖️  Apagado cancelado", color=0x95a5a6),
            view=None,
        )

# ──────────────────────────────────────────
# VIEW — Panel de apagado
# ──────────────────────────────────────────
class ShutdownPanel(discord.ui.View):
    def __init__(self, nas_online: bool, media_online: bool):
        super().__init__(timeout=300)
        self.nas_online   = nas_online
        self.media_online = media_online
        self._build()

    def _build(self):
        self.clear_items()

        nas_btn = discord.ui.Button(
            label=f"Apagar {SERVERS['nas']['name']}" if self.nas_online
                  else f"{SERVERS['nas']['name']} OFFLINE",
            style=discord.ButtonStyle.danger if self.nas_online else discord.ButtonStyle.secondary,
            emoji="🗄️",
            disabled=not self.nas_online,
        )
        nas_btn.callback = self._shutdown_nas
        self.add_item(nas_btn)

        media_btn = discord.ui.Button(
            label=f"Apagar {SERVERS['media']['name']}" if self.media_online
                  else f"{SERVERS['media']['name']} OFFLINE",
            style=discord.ButtonStyle.danger if self.media_online else discord.ButtonStyle.secondary,
            emoji="📺",
            disabled=not self.media_online,
        )
        media_btn.callback = self._shutdown_media
        self.add_item(media_btn)

    async def _ask(self, interaction: discord.Interaction, server_key: str):
        srv = SERVERS[server_key]
        note = ""
        if server_key == "media":
            note = "\n\n⚠️  El *failsafe* podría reencenderlo si estás dentro de la franja horaria. Pausalo desde `/schedule` si querés que quede apagado."
        embed = discord.Embed(
            title=f"⚠️  ¿Apagar {srv['name']}?",
            description=(
                f"Vas a enviar `sudo shutdown -h now` a **{srv['name']}** (`{srv['ip']}`).\n"
                f"Se van a cortar sus servicios hasta que lo despiertes con WOL."
                f"{note}"
            ),
            color=0xe67e22,
        )
        await interaction.response.send_message(
            embed=embed, view=ShutdownConfirmView(server_key), ephemeral=True
        )

    async def _shutdown_nas(self, interaction: discord.Interaction):
        await self._ask(interaction, "nas")

    async def _shutdown_media(self, interaction: discord.Interaction):
        await self._ask(interaction, "media")

# ──────────────────────────────────────────
# VIEW — Panel WOL principal
# ──────────────────────────────────────────
class WOLPanel(discord.ui.View):
    def __init__(self, nas_online: bool, media_online: bool):
        super().__init__(timeout=300)
        self.nas_online   = nas_online
        self.media_online = media_online
        self._update_buttons()

    def _update_buttons(self):
        self.clear_items()

        nas_btn = discord.ui.Button(
            label=f"Despertar {SERVERS['nas']['name']}" if not self.nas_online
                  else f"{SERVERS['nas']['name']} ya está ONLINE",
            style=discord.ButtonStyle.success if not self.nas_online else discord.ButtonStyle.secondary,
            emoji="🗄️",
            custom_id="wake_nas",
            disabled=self.nas_online,
        )
        nas_btn.callback = self.wake_nas
        self.add_item(nas_btn)

        media_btn = discord.ui.Button(
            label=f"Despertar {SERVERS['media']['name']}" if not self.media_online
                  else f"{SERVERS['media']['name']} ya está ONLINE",
            style=discord.ButtonStyle.success if not self.media_online else discord.ButtonStyle.secondary,
            emoji="📺",
            custom_id="wake_media",
            disabled=self.media_online,
        )
        media_btn.callback = self.wake_media
        self.add_item(media_btn)

        refresh_btn = discord.ui.Button(
            label="Actualizar estado",
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            custom_id="refresh",
        )
        refresh_btn.callback = self.refresh_status
        self.add_item(refresh_btn)

    async def _wake(self, interaction: discord.Interaction, server_key: str):
        srv = SERVERS[server_key]

        already_on = await check_status(srv["ip"])
        if already_on:
            await interaction.response.send_message(
                f"ℹ️ `{srv['name']}` ya está ONLINE.", ephemeral=True
            )
            return

        sent = await asyncio.to_thread(send_wol, srv["mac"])
        if not sent:
            await interaction.response.send_message(
                "❌ No se encontró `wakeonlan`.\nInstalá: `sudo pacman -S wakeonlan`",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📡  Magic packet enviado → {srv['name']}",
            description=f"MAC: `{srv['mac']}`",
            color=0x3498db,
            timestamp=datetime.now(),
        )
        embed.set_footer(text="Monitoreando arranque...")

        await interaction.response.send_message(
            f"⚡ Despertando **{srv['name']}**...", ephemeral=True
        )
        monitor_msg = await interaction.channel.send(embed=embed)
        asyncio.create_task(monitor_boot(monitor_msg, server_key))

    async def wake_nas(self, interaction: discord.Interaction):
        await self._wake(interaction, "nas")

    async def wake_media(self, interaction: discord.Interaction):
        await self._wake(interaction, "media")

    async def refresh_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        nas_online, media_online = await asyncio.gather(
            check_status(SERVERS["nas"]["ip"]),
            check_status(SERVERS["media"]["ip"]),
        )
        self.nas_online   = nas_online
        self.media_online = media_online
        self._update_buttons()
        embed = build_panel_embed(nas_online, media_online)
        await interaction.message.edit(embed=embed, view=self)

# ──────────────────────────────────────────
# BOT
# ──────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot  = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)

@bot.event
async def on_ready():
    print(f"WOL Bot ONLINE: {bot.user}")
    await tree.sync()
    asyncio.create_task(schedule_loop())
    asyncio.create_task(failsafe_loop())

    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    nas_online, media_online = await asyncio.gather(
        check_status(SERVERS["nas"]["ip"]),
        check_status(SERVERS["media"]["ip"]),
    )
    embed = build_panel_embed(nas_online, media_online)
    view  = WOLPanel(nas_online, media_online)
    await channel.send(embed=embed, view=view)

@tree.command(name="wol", description="Abre el panel Wake-on-LAN")
async def wol_command(interaction: discord.Interaction):
    nas_online, media_online = await asyncio.gather(
        check_status(SERVERS["nas"]["ip"]),
        check_status(SERVERS["media"]["ip"]),
    )
    embed = build_panel_embed(nas_online, media_online)
    view  = WOLPanel(nas_online, media_online)
    await interaction.response.send_message(embed=embed, view=view)

@tree.command(name="schedule", description="Gestionar el horario automático del servidor multimedia")
async def schedule_command(interaction: discord.Interaction):
    cfg   = load_schedule()
    embed = build_schedule_embed(cfg)
    view  = SchedulePanel(cfg)
    await interaction.response.send_message(embed=embed, view=view)

@tree.command(name="reboot", description="Reiniciar uno de los servidores (vía SSH)")
async def reboot_command(interaction: discord.Interaction):
    nas_online, media_online = await asyncio.gather(
        check_status(SERVERS["nas"]["ip"]),
        check_status(SERVERS["media"]["ip"]),
    )
    embed = discord.Embed(
        title="🔄  Reboot Control",
        description="Elegí el servidor a reiniciar. Solo se pueden reiniciar los ONLINE.",
        color=0xe67e22,
        timestamp=datetime.now(),
    )
    embed.add_field(
        name=f"🗄️  {SERVERS['nas']['name']}",
        value=f"{status_line(nas_online)}\n`{SERVERS['nas']['ip']}`",
        inline=True,
    )
    embed.add_field(
        name=f"📺  {SERVERS['media']['name']}",
        value=f"{status_line(media_online)}\n`{SERVERS['media']['ip']}`",
        inline=True,
    )
    view = RebootPanel(nas_online, media_online)
    await interaction.response.send_message(embed=embed, view=view)

@tree.command(name="shutdown", description="Apagar uno de los servidores (vía SSH)")
async def shutdown_command(interaction: discord.Interaction):
    nas_online, media_online = await asyncio.gather(
        check_status(SERVERS["nas"]["ip"]),
        check_status(SERVERS["media"]["ip"]),
    )
    embed = discord.Embed(
        title="🌙  Shutdown Control",
        description="Elegí el servidor a apagar. Solo se pueden apagar los ONLINE.",
        color=0xe67e22,
        timestamp=datetime.now(),
    )
    embed.add_field(
        name=f"🗄️  {SERVERS['nas']['name']}",
        value=f"{status_line(nas_online)}\n`{SERVERS['nas']['ip']}`",
        inline=True,
    )
    embed.add_field(
        name=f"📺  {SERVERS['media']['name']}",
        value=f"{status_line(media_online)}\n`{SERVERS['media']['ip']}`",
        inline=True,
    )
    view = ShutdownPanel(nas_online, media_online)
    await interaction.response.send_message(embed=embed, view=view)

# ──────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────
if not TOKEN:
    print("ERROR: Falta DISCORD_TOKEN en .env")
    sys.exit(1)

bot.run(TOKEN)
