"""Persistencia del horario y aritmética de la franja horaria.

Vive separado de `scheduler.py` porque `wolctl.py` necesita saber si un
servidor *debería* estar encendido, y `scheduler.py` importa discord, embeds y
monitors. Una automatización local que solo quiere consultar la franja no tiene
por qué arrastrar media librería de Discord.

`scheduler.py` reexporta todo lo de acá: la definición de la franja sigue
existiendo una sola vez.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from datetime import time as dtime

import config


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


def should_be_online(server_key: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """¿El horario dice que este server tendría que estar encendido ahora?

    Un horario deshabilitado NO es "siempre encendido": es "el bot no maneja el
    encendido de este server". Devuelve False, y quien haya encendido el server
    es responsable de devolverlo a como estaba. Es la diferencia entre respetar
    una franja y dejar un nodo prendido toda la noche porque nadie configuró una.
    """
    cfg = load_schedule(server_key)
    if not cfg.get("enabled"):
        return False, "el horario automático está deshabilitado (no hay franja)"

    now = now or datetime.now()
    try:
        wake_t = parse_hhmm(cfg["wake_time"])
        shut_t = parse_hhmm(cfg["shutdown_time"])
    except (KeyError, ValueError) as exc:
        # Un horario ilegible no autoriza a apagar nada.
        return True, f"horario ilegible ({exc}); por las dudas se lo deja encendido"

    if in_uptime_window(now.time(), wake_t, shut_t):
        return True, f"dentro de la franja {cfg['wake_time']}–{cfg['shutdown_time']}"
    return False, f"fuera de la franja {cfg['wake_time']}–{cfg['shutdown_time']}"
