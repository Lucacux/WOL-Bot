"""Construcción de todos los embeds de Discord.

Sin lógica de negocio ni I/O: reciben datos ya resueltos (estados, cfg) y
devuelven `discord.Embed`. Todos iteran sobre `config.SERVERS`, así que sumar
un servidor no toca este archivo.
"""
from datetime import datetime

import discord

import config


def status_line(online: bool) -> str:
    return "🟢 ONLINE" if online else "🔴 OFFLINE"


def _server_field(embed: discord.Embed, key: str, online: bool, inline: bool = True):
    srv = config.SERVERS[key]
    embed.add_field(
        name=f"{srv['emoji']}  {srv['name']}",
        value=f"{status_line(online)}\n`{srv['ip']}`",
        inline=inline,
    )


# ──────────────────────────────────────────
# PANEL WOL
# ──────────────────────────────────────────
def build_panel_embed(statuses: dict) -> discord.Embed:
    all_online = all(statuses.values())
    embed = discord.Embed(
        title="🖥️  WOL Control Panel",
        color=0x2ecc71 if all_online else 0xe67e22,
        timestamp=datetime.now(),
    )
    for key in config.SERVERS:
        _server_field(embed, key, statuses.get(key, False))
    embed.set_footer(text="Usá los botones para despertar un servidor")
    return embed


def build_control_embed(title: str, description: str, statuses: dict) -> discord.Embed:
    """Embed genérico para los paneles /reboot y /shutdown."""
    embed = discord.Embed(
        title=title,
        description=description,
        color=0xe67e22,
        timestamp=datetime.now(),
    )
    for key in config.SERVERS:
        _server_field(embed, key, statuses.get(key, False))
    return embed


# ──────────────────────────────────────────
# HORARIO
# ──────────────────────────────────────────
def build_schedule_embed(server_key: str, cfg: dict) -> discord.Embed:
    srv     = config.SERVERS[server_key]
    enabled = cfg["enabled"]
    embed = discord.Embed(
        title=f"📅  Horario Automático — {srv['name']}",
        description=f"Estado: {'🟢 **Activo**' if enabled else '⏸️  **Pausado**'}",
        color=0x2ecc71 if enabled else 0x95a5a6,
        timestamp=datetime.now(),
    )
    embed.add_field(name="⏰  Encendido", value=f"`{cfg['wake_time']}`",     inline=True)
    embed.add_field(name="🌙  Apagado",   value=f"`{cfg['shutdown_time']}`", inline=True)
    embed.add_field(name="​",  value="​",              inline=True)

    # Las marcas de apagado/cancelado se anclan a la ventana (last_wake_date),
    # así que las comparamos contra last_wake, no contra el día calendario.
    from datetime import date
    today     = date.today().isoformat()
    last_wake = cfg.get("last_wake_date")
    notes = []
    if last_wake == today:
        notes.append("✅ WOL enviado hoy")
    if last_wake and cfg.get("last_shutdown_date") == last_wake:
        if cfg.get("shutdown_cancelled_date") == last_wake:
            notes.append("⚠️  Apagado cancelado manualmente — se retoma mañana")
        else:
            notes.append("✅ Apagado ejecutado")
    if not notes:
        notes.append("Sin acciones ejecutadas hoy")
    embed.add_field(name="📋  Actividad de hoy", value="\n".join(notes), inline=False)

    fs = cfg.get("failsafe_enabled", True)
    embed.add_field(
        name="🛟  Failsafe (watchdog WOL)",
        value=(
            f"{'🟢 **Activo**' if fs else '⏸️  **Pausado**'} — reenciende solo "
            f"**{srv['name']}** si se cae dentro de la franja "
            f"`{cfg['wake_time']}`–`{cfg['shutdown_time']}`."
        ),
        inline=False,
    )
    embed.set_footer(text=f"Configuración aplica a: {srv['name']}")
    return embed


def build_shutdown_warning_embed(server_key: str, shutdown_time: str, remaining: float) -> discord.Embed:
    srv    = config.SERVERS[server_key]
    mins   = int(remaining // 60)
    secs   = int(remaining % 60)
    filled = max(0, min(16, int((1.0 - remaining / config.SHUTDOWN_WARN_SECS) * 16)))
    bar    = "█" * filled + "░" * (16 - filled)

    embed = discord.Embed(
        title=f"⚠️  Apagado automático en {mins:02d}:{secs:02d}",
        description=(
            f"**{srv['name']}** se apaga a las `{shutdown_time}` según el horario programado.\n"
            f"Presioná **No apagar** para cancelar por esta noche."
        ),
        color=0xe67e22,
        timestamp=datetime.now(),
    )
    embed.add_field(name="⏱️  ETA", value=f"`{bar}` **{mins:02d}:{secs:02d}**", inline=False)
    embed.set_footer(text="Sin respuesta → apagado automático")
    return embed


# ──────────────────────────────────────────
# FAILSAFE
# ──────────────────────────────────────────
def build_failsafe_alert_embed(server_key: str, attempt: int, wol_ok: bool, slow_mode: bool) -> discord.Embed:
    srv = config.SERVERS[server_key]
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
            value=f"`lento` — reintenta cada {config.FAILSAFE_SLOW_COOLDOWN // 60} min",
            inline=True,
        )
        embed.set_footer(text="Sin respuesta tras varios intentos (¿apagón?). Seguirá vigilando.")
    else:
        embed.set_footer(text="Monitoreando la recuperación...")
    return embed


def build_failsafe_recovered_embed(server_key: str, downtime_secs: float, attempts: int) -> discord.Embed:
    srv  = config.SERVERS[server_key]
    mins = int(downtime_secs // 60)
    secs = int(downtime_secs % 60)
    return discord.Embed(
        title=f"✅  Failsafe: {srv['name']} recuperado",
        description=(
            f"El servidor volvió a estar ONLINE tras `{mins:02d}:{secs:02d}` "
            f"de caída y **{attempts}** intento(s) de WOL."
        ),
        color=0x2ecc71,
        timestamp=datetime.now(),
    )
