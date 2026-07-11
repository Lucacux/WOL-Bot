"""Horario automático y failsafe, POR SERVIDOR.

Persistencia (`schedule.json`), parsing de horas, y los dos background loops:

  · schedule_loop  → enciende (WOL) y apaga (SSH) cada server según su franja.
  · failsafe_loop  → si un server se cae DENTRO de su franja activa, reenvía WOL.

Ambos loops iteran sobre `config.SERVERS`: la lógica es idéntica para NAS y
Media (y para cualquier server que agregues), cada uno con su propia config y
su propio estado de failsafe.
"""
import os
import json
import time as _time
import asyncio
from datetime import datetime, time as dtime, date, timedelta

import discord

import config
from network import check_status, wake, is_server_down, ssh_shutdown
from embeds import (
    build_shutdown_warning_embed,
    build_failsafe_alert_embed,
    build_failsafe_recovered_embed,
)
from monitors import monitor_boot


# ──────────────────────────────────────────
# PERSISTENCIA — schedule.json (por servidor)
# ──────────────────────────────────────────
def _default_all() -> dict:
    return {key: dict(config.DEFAULT_SERVER_SCHEDULE) for key in config.SERVERS}


def load_schedules() -> dict:
    """Devuelve {server_key: cfg} para TODOS los servidores.

    Migra el formato viejo (un único dict plano con `wake_time`, `enabled`, …
    que era solo del Homeserver Multimedia) al nuevo formato anidado por
    servidor, sin perder la configuración viva. Todo server que falte en el
    archivo se completa con DEFAULT_SERVER_SCHEDULE.
    """
    raw = {}
    if config.SCHEDULE_FILE and os.path.exists(config.SCHEDULE_FILE):
        try:
            with open(config.SCHEDULE_FILE, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"[schedule] Error leyendo {config.SCHEDULE_FILE}: {e}")
            raw = {}

    # Formato legado: dict plano con las claves de un schedule → era de "media".
    if isinstance(raw, dict) and "wake_time" in raw:
        raw = {"media": raw}

    result = {}
    for key in config.SERVERS:
        cfg = dict(config.DEFAULT_SERVER_SCHEDULE)
        stored = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(stored, dict):
            cfg.update(stored)
        result[key] = cfg
    return result


def save_schedules(all_cfg: dict):
    with open(config.SCHEDULE_FILE, "w", encoding="utf-8") as f:
        json.dump(all_cfg, f, indent=2, ensure_ascii=False)


def load_schedule(server_key: str) -> dict:
    return load_schedules()[server_key]


def save_schedule(server_key: str, cfg: dict):
    """Guarda la config de UN servidor haciendo read-merge para no pisar los
    otros (los loops y las Views escriben poco y espaciado; el merge alcanza)."""
    all_cfg = load_schedules()
    all_cfg[server_key] = cfg
    save_schedules(all_cfg)


def parse_hhmm(t: str) -> dtime:
    parts = t.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"Formato inválido: {t!r}")
    return dtime(int(parts[0]), int(parts[1]))


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


# ──────────────────────────────────────────
# AVISO + APAGADO PROGRAMADO
# ──────────────────────────────────────────
async def run_shutdown_warning(bot, server_key: str, cfg: dict, anchor: str):
    # `anchor` = fecha de la ventana (last_wake_date). Se usa para marcar la
    # cancelación contra la MISMA ventana que el disparo, para que la guarda no
    # se reabra al rotar el día calendario a medianoche.
    from views import CancelShutdownView  # import perezoso: evita ciclo views↔scheduler

    srv     = config.SERVERS[server_key]
    channel = bot.get_channel(config.CHANNEL_ID)
    if not channel:
        return

    view  = CancelShutdownView()
    embed = build_shutdown_warning_embed(server_key, cfg["shutdown_time"], config.SHUTDOWN_WARN_SECS)
    msg   = await channel.send(embed=embed, view=view)

    start = datetime.now()

    while True:
        elapsed   = (datetime.now() - start).total_seconds()
        remaining = max(0.0, config.SHUTDOWN_WARN_SECS - elapsed)

        if view.cancelled:
            cur = load_schedule(server_key)
            cur["shutdown_cancelled_date"] = anchor
            save_schedule(server_key, cur)
            await msg.edit(
                embed=discord.Embed(
                    title="✅  Apagado cancelado por esta noche",
                    description=(
                        f"**{srv['name']}** seguirá encendido.\n"
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
            embed=build_shutdown_warning_embed(server_key, cfg["shutdown_time"], remaining),
            view=view,
        )
        await asyncio.sleep(config.SHUTDOWN_WARN_REFRESH)

    # Tiempo agotado → apagar
    await msg.edit(
        embed=discord.Embed(
            title=f"🔌  Apagando {srv['name']}...",
            description="Enviando comando de apagado vía SSH...",
            color=0xe74c3c,
            timestamp=datetime.now(),
        ),
        view=None,
    )

    success = await ssh_shutdown(server_key)

    await msg.edit(
        embed=discord.Embed(
            title="🔴  Servidor apagado" if success else "❌  Error al apagar",
            description=(
                f"**{srv['name']}** fue apagado automáticamente."
                if success else
                "No se pudo conectar por SSH. Verificá la clave y el permiso de sudo."
            ),
            color=0x992d22 if success else 0xff0000,
            timestamp=datetime.now(),
        )
    )


# ──────────────────────────────────────────
# SCHEDULE LOOP — encendido/apagado por servidor
# ──────────────────────────────────────────
async def _process_schedule(bot, server_key: str, cfg: dict, now: datetime) -> bool:
    """Procesa la franja de UN servidor. Devuelve True si mutó `cfg`."""
    srv     = config.SERVERS[server_key]
    today   = now.date().isoformat()
    dirty   = False

    wake_t = parse_hhmm(cfg["wake_time"])
    shut_t = parse_hhmm(cfg["shutdown_time"])

    # ── ENCENDIDO ──
    if now.time() >= wake_t and cfg.get("last_wake_date") != today:
        cfg["last_wake_date"] = today
        dirty = True

        online = await check_status(srv["ip"])
        if not online:
            wol_ok  = await wake(server_key)
            channel = bot.get_channel(config.CHANNEL_ID)
            if channel:
                if wol_ok:
                    await channel.send(embed=discord.Embed(
                        title="⏰  Encendido automático",
                        description=(
                            f"Magic packet enviado a **{srv['name']}**.\n"
                            f"Horario configurado: `{cfg['wake_time']}`"
                        ),
                        color=0x3498db,
                        timestamp=datetime.now(),
                    ))
                    monitor_msg = await channel.send(embed=discord.Embed(
                        title=f"⏳  Iniciando: {srv['name']}",
                        color=0x3498db,
                        timestamp=datetime.now(),
                    ))
                    asyncio.create_task(monitor_boot(monitor_msg, server_key))
                else:
                    await channel.send(embed=discord.Embed(
                        title="❌  Error en encendido automático",
                        description=(
                            f"No se pudo enviar el magic packet a **{srv['name']}**.\n"
                            f"Verificá que `wakeonlan` esté instalado."
                        ),
                        color=0xe74c3c,
                        timestamp=datetime.now(),
                    ))

    # ── APAGADO ──
    # El apagado se ancla a la FECHA real del último encendido, no a "hoy a las
    # shut_t". Si shutdown_time <= wake_time la ventana cruza medianoche y el
    # apagado cae al día siguiente. Las guardas de "ya resuelto" se anclan a la
    # MISMA ventana (last_wake), NO a `today`: si no, a las 00:00 `today` rota y
    # la guarda se reabre re-disparando un apagado ya ejecutado/cancelado.
    last_wake = cfg.get("last_wake_date")
    if last_wake:
        shutdown_dt = datetime.combine(date.fromisoformat(last_wake), shut_t)
        if shut_t <= wake_t:
            shutdown_dt += timedelta(days=1)
        warn_dt = shutdown_dt - timedelta(seconds=config.SHUTDOWN_WARN_SECS)

        already_done      = cfg.get("last_shutdown_date")      == last_wake
        already_cancelled = cfg.get("shutdown_cancelled_date") == last_wake
        too_late          = now >= shutdown_dt + timedelta(seconds=config.SHUTDOWN_MAX_LATE_SECS)

        if now >= warn_dt and not already_done and not already_cancelled:
            if too_late:
                # El bot estuvo caído toda la ventana y arrancó horas después:
                # consumimos la franja sin apagar nada para no sorprender.
                cfg["last_shutdown_date"] = last_wake
                dirty = True
                print(f"[schedule:{server_key}] Apagado de {last_wake} vencido "
                      f"(>{config.SHUTDOWN_MAX_LATE_SECS // 3600}h tarde); se omite.")
            else:
                cfg["last_shutdown_date"] = last_wake
                dirty = True
                asyncio.create_task(run_shutdown_warning(bot, server_key, cfg, last_wake))

    return dirty


async def schedule_loop(bot):
    await bot.wait_until_ready()
    print("[schedule] Loop iniciado.")

    while not bot.is_closed():
        try:
            all_cfg = load_schedules()
            now     = datetime.now()
            dirty   = False

            for server_key, cfg in all_cfg.items():
                if not cfg.get("enabled"):
                    continue
                if await _process_schedule(bot, server_key, cfg, now):
                    dirty = True

            if dirty:
                save_schedules(all_cfg)

        except Exception as e:
            print(f"[schedule_loop] Excepción: {e}")

        await asyncio.sleep(config.SCHEDULE_CHECK_SECS)


# ──────────────────────────────────────────
# FAILSAFE LOOP — watchdog de encendido por servidor
# ──────────────────────────────────────────
# Estado independiente por servidor.
_failsafe = {
    key: {
        "active":       False,   # hay una caída en curso siendo atendida
        "outage_start": None,    # _time.monotonic() del inicio de la caída
        "last_wol":     None,    # _time.monotonic() del último WOL enviado
        "attempts":     0,       # WOLs enviados en esta caída
        "window_since": None,    # _time.monotonic() en que entramos a la franja
    }
    for key in config.SERVERS
}


def _reset_failsafe(server_key: str, keep_window: bool = False):
    st  = _failsafe[server_key]
    win = st["window_since"] if keep_window else None
    st.update(active=False, outage_start=None, last_wol=None, attempts=0, window_since=win)


async def _process_failsafe(bot, server_key: str, cfg: dict):
    st = _failsafe[server_key]

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
        if st["active"] or st["window_since"] is not None:
            _reset_failsafe(server_key)
        return

    # Marca de entrada a la franja (para la gracia de arranque).
    if st["window_since"] is None:
        st["window_since"] = _time.monotonic()

    down = await is_server_down(server_key)

    if not down:
        # ONLINE. Si veníamos de una caída atendida → avisar recuperación.
        if st["active"]:
            channel = bot.get_channel(config.CHANNEL_ID)
            if channel:
                downtime = _time.monotonic() - (st["outage_start"] or _time.monotonic())
                await channel.send(embed=build_failsafe_recovered_embed(
                    server_key, downtime, st["attempts"],
                ))
        _reset_failsafe(server_key, keep_window=True)
        return

    # ── CAÍDA CONFIRMADA dentro de la franja ──
    now_m = _time.monotonic()

    # Gracia al recién entrar en franja (o tras reiniciar el bot): dejamos que
    # el encendido programado y el boot ocurran sin duplicar WOL ni gritar una
    # falsa alarma.
    if now_m - st["window_since"] < config.FAILSAFE_BOOT_GRACE:
        return

    if not st["active"]:
        st["active"]       = True
        st["outage_start"] = now_m

    slow_mode = st["attempts"] >= config.FAILSAFE_MAX_FAST_TRIES
    cooldown  = config.FAILSAFE_SLOW_COOLDOWN if slow_mode else config.FAILSAFE_WOL_COOLDOWN
    due = st["last_wol"] is None or (now_m - st["last_wol"]) >= cooldown

    if due:
        wol_ok = await wake(server_key)
        st["last_wol"] = now_m
        st["attempts"] += 1
        channel = bot.get_channel(config.CHANNEL_ID)
        if channel:
            await channel.send(embed=build_failsafe_alert_embed(
                server_key, st["attempts"], wol_ok, slow_mode,
            ))


async def failsafe_loop(bot):
    await bot.wait_until_ready()
    print("[failsafe] Loop iniciado.")

    while not bot.is_closed():
        try:
            all_cfg = load_schedules()
            for server_key, cfg in all_cfg.items():
                await _process_failsafe(bot, server_key, cfg)
        except Exception as e:
            print(f"[failsafe_loop] Excepción: {e}")

        await asyncio.sleep(config.FAILSAFE_CHECK_SECS)
