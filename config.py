"""Configuración centralizada del WOL-Bot, toda por variables de entorno.

Ningún secreto vive en el repo. En el host los valores van en el `.env`
(gitignored); en Dokploy irían en el env de la aplicación.

Este módulo define QUÉ servidores maneja el bot y CON QUÉ parámetros. Agregar
un servidor nuevo es agregar una entrada en `SERVERS` (+ su `SSH_CONFIG`): el
resto del bot (paneles, horario, failsafe) itera sobre `SERVERS`, así que la
lógica no hay que tocarla.
"""
import os
from dotenv import load_dotenv

load_dotenv()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# ──────────────────────────────────────────
# DISCORD
# ──────────────────────────────────────────
TOKEN         = os.getenv('DISCORD_TOKEN')
CHANNEL_ID    = int(os.getenv('DISCORD_CHANNEL_ID', '0'))
WOL_INTERFACE = os.getenv('WOL_INTERFACE', 'eth0.20')

# ──────────────────────────────────────────
# SERVIDORES
# ──────────────────────────────────────────
# `key` interno → metadata de red + presentación. El orden acá es el orden en
# que aparecen los botones/campos en los embeds.
SERVERS = {
    "nas": {
        "name":  os.getenv('NAME_NAS',  'NAS Fileserver'),
        "mac":   os.getenv('MAC_NAS',   '00:13:8F:98:6A:08'),
        "ip":    os.getenv('IP_NAS',    '192.168.2.20'),
        "emoji": os.getenv('EMOJI_NAS', '🗄️'),
    },
    "media": {
        "name":  os.getenv('NAME_MEDIA',  'Homeserver Multimedia'),
        "mac":   os.getenv('MAC_MEDIA',   '84:2B:2B:7F:44:33'),
        "ip":    os.getenv('IP_MEDIA',    '192.168.2.10'),
        "emoji": os.getenv('EMOJI_MEDIA', '📺'),
    },
}

# ── SSH por servidor (shutdown / reboot) ──
# NAS cae por defecto a la misma credencial que Media salvo override explícito,
# para no obligar a duplicar variables si compartís usuario/clave.
_SSH_USER_MEDIA = os.getenv('SSH_USER_MEDIA', 'luca')
_SSH_KEY_MEDIA  = os.getenv('SSH_KEY_MEDIA',  os.path.expanduser('~/.ssh/id_ed25519_wol'))
_SSH_PORT_MEDIA = os.getenv('SSH_PORT_MEDIA', '2222')

SSH_CONFIG = {
    "nas": {
        "user": os.getenv('SSH_USER_NAS', _SSH_USER_MEDIA),
        "key":  os.getenv('SSH_KEY_NAS',  _SSH_KEY_MEDIA),
        "port": os.getenv('SSH_PORT_NAS', '22'),
    },
    "media": {
        "user": _SSH_USER_MEDIA,
        "key":  _SSH_KEY_MEDIA,
        "port": _SSH_PORT_MEDIA,
    },
}

# ──────────────────────────────────────────
# SCHEDULE (horario automático por servidor)
# ──────────────────────────────────────────
SCHEDULE_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.json")
SCHEDULE_CHECK_SECS   = 30    # frecuencia del loop de verificación
SHUTDOWN_WARN_SECS    = 120   # ventana de aviso antes del apagado (2 minutos)
SHUTDOWN_WARN_REFRESH = 10    # refresca el countdown cada N segundos
# Tope de retraso: si el bot estuvo caído durante toda la ventana y arranca
# mucho después de la hora de apagado, NO disparamos un apagado sorpresa. Un
# reinicio/deploy normal cae muy por debajo de esto; horas después, se descarta.
SHUTDOWN_MAX_LATE_SECS = 2 * 3600  # 2 h

# Estado por servidor. `enabled` arranca en False: el horario es OPT-IN, así un
# deploy nuevo nunca apaga un server por sorpresa. La instalación viva conserva
# su valor real vía la migración en scheduler.load_schedules().
DEFAULT_SERVER_SCHEDULE = {
    "enabled":                 False,
    "wake_time":               "06:30",
    "shutdown_time":           "23:00",
    "last_wake_date":          None,
    "last_shutdown_date":      None,
    "shutdown_cancelled_date": None,
    "failsafe_enabled":        True,   # watchdog WOL dentro de la franja activa
}

# ──────────────────────────────────────────
# FAILSAFE (watchdog de encendido)
# ──────────────────────────────────────────
# Si un servidor está caído dentro de su franja "debería-estar-encendido"
# (wake_time → shutdown_time), el failsafe reenvía WOL solo. Ping controlado:
# en estado normal es 1 paquete ICMP por ciclo; solo escala a varios pings
# cuando el primero falla, para confirmar la caída.
FAILSAFE_CHECK_SECS     = _env_int('FAILSAFE_CHECK_SECS', 60)    # cadencia del watchdog
FAILSAFE_CONFIRM_CHECKS = _env_int('FAILSAFE_CONFIRM_CHECKS', 3) # pings consecutivos fallidos = caída
FAILSAFE_CONFIRM_GAP    = _env_int('FAILSAFE_CONFIRM_GAP', 5)    # segundos entre pings de confirmación
FAILSAFE_WOL_COOLDOWN   = _env_int('FAILSAFE_WOL_COOLDOWN', 180) # espera tras un WOL (deja bootear)
FAILSAFE_MAX_FAST_TRIES = _env_int('FAILSAFE_MAX_FAST_TRIES', 3) # intentos antes de pasar a modo lento
FAILSAFE_SLOW_COOLDOWN  = _env_int('FAILSAFE_SLOW_COOLDOWN', 600)# cooldown en modo lento (ej. apagón)
FAILSAFE_BOOT_GRACE     = _env_int('FAILSAFE_BOOT_GRACE', 150)   # gracia al entrar en franja / reinicio
