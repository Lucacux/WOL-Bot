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
    # El hipervisor NO vive en la misma VLAN que el bot, y eso rompe los dos
    # supuestos que valen para nas/media (broadcast L2 + ICMP). De ahí los dos
    # overrides; ver el bloque "SERVIDORES EN OTRA VLAN" más abajo.
    "proxmox": {
        "name":       os.getenv('NAME_PROXMOX',  'Proxmox (pve)'),
        "mac":        os.getenv('MAC_PROXMOX',   '70:85:C2:BD:C8:53'),
        "ip":         os.getenv('IP_PROXMOX',    '192.168.1.70'),
        "emoji":      os.getenv('EMOJI_PROXMOX', '🖥️'),
        "wol_target": os.getenv('WOL_TARGET_PROXMOX', '192.168.1.70'),
        "probe":      os.getenv('PROBE_PROXMOX',      'tcp'),
        "probe_port": _env_int('PROBE_PORT_PROXMOX',  22),
    },
}

# ──────────────────────────────────────────
# SERVIDORES EN OTRA VLAN
# ──────────────────────────────────────────
# El bot corre en 192.168.2.40 (VLAN 20, "servidores"). Un servidor en otra
# VLAN necesita dos cosas que los locales no:
#
# 1. `wol_target` — el magic packet por defecto sale a 255.255.255.255, que es
#    *limited broadcast*: ningún router lo reenvía, nunca. Apuntando el paquete
#    a la IP del server sale como unicast y el router SÍ lo rutea. La NIC lo
#    reconoce igual: el magic packet se detecta por patrón, no por ser
#    broadcast. Requiere en el OpenWRT una ARP permanente para esa IP (con la
#    máquina apagada no hay quién responda el ARP) y una regla que deje pasar
#    UDP/9 desde el bot. Sin `wol_target` se usa el broadcast de siempre.
#
# 2. `probe` — el gateway descarta el ICMP entre VLANs, así que un ping da
#    siempre "caído" y el failsafe quedaría reenviando WOL en loop. Con
#    `probe: "tcp"` la sonda es un connect a `probe_port`, que además prueba
#    algo más fuerte: que el sshd levantó, no que el kernel contesta ARP.
#    Sin `probe` se usa ICMP, como siempre.
PROBE_TCP_TIMEOUT = _env_int('PROBE_TCP_TIMEOUT', 3)  # segundos por intento
WOL_DEFAULT_PORT  = _env_int('WOL_DEFAULT_PORT', 9)

# ── SSH por servidor (shutdown / reboot) ──
# NAS cae por defecto a la misma credencial que Media salvo override explícito,
# para no obligar a duplicar variables si compartís usuario/clave.
_SSH_USER_MEDIA = os.getenv('SSH_USER_MEDIA', 'luca')
_SSH_KEY_MEDIA  = os.getenv('SSH_KEY_MEDIA',  os.path.expanduser('~/.ssh/id_ed25519_wol'))
_SSH_PORT_MEDIA = os.getenv('SSH_PORT_MEDIA', '2222')

# Comandos remotos por defecto. Un servidor puede pisarlos: el Proxmox entra con
# una clave de *comando forzado*, que no ejecuta lo que se le mande sino que lee
# $SSH_ORIGINAL_COMMAND y solo acepta las palabras `halt` y `reboot`. Así la
# clave del bot no sirve para nada más que prender y apagar.
DEFAULT_SHUTDOWN_CMD = "sudo shutdown -h now"
DEFAULT_REBOOT_CMD   = "sudo shutdown -r now"

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
    "proxmox": {
        "user":         os.getenv('SSH_USER_PROXMOX', 'wol-bot'),
        "key":          os.getenv('SSH_KEY_PROXMOX',  _SSH_KEY_MEDIA),
        "port":         os.getenv('SSH_PORT_PROXMOX', '22'),
        "shutdown_cmd": os.getenv('SSH_SHUTDOWN_CMD_PROXMOX', 'halt'),
        "reboot_cmd":   os.getenv('SSH_REBOOT_CMD_PROXMOX',   'reboot'),
    },
}


def shutdown_cmd(server_key: str) -> str:
    return SSH_CONFIG[server_key].get("shutdown_cmd") or DEFAULT_SHUTDOWN_CMD


def reboot_cmd(server_key: str) -> str:
    return SSH_CONFIG[server_key].get("reboot_cmd") or DEFAULT_REBOOT_CMD

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

# Reservas cooperativas para tareas externas (por ejemplo Updates-Bot). Una
# reserva activa impide que el scheduler apague el servidor a mitad de una
# operación. Tienen vencimiento obligatorio: si el cliente muere sin liberar,
# el apagado vuelve a habilitarse solo.
MAINTENANCE_FILE = os.getenv(
    "MAINTENANCE_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "maintenance.json"),
)
MAINTENANCE_MAX_TTL_SECS = _env_int("MAINTENANCE_MAX_TTL_SECS", 6 * 3600)

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

# Semilla por servidor: pisa DEFAULT_SERVER_SCHEDULE la PRIMERA vez que el
# servidor aparece en schedule.json. Una vez guardado, manda lo persistido —
# si pausás el horario desde /schedule, la semilla no lo vuelve a encender.
# El Proxmox arranca con la franja puesta y activa, por decisión explícita:
# el resto de los servers quedaron opt-in porque se agregaron antes de que el
# horario estuviera probado.
SERVER_SCHEDULE_SEED = {
    "proxmox": {
        "enabled":       True,
        "wake_time":     os.getenv('WAKE_TIME_PROXMOX',     '08:00'),
        "shutdown_time": os.getenv('SHUTDOWN_TIME_PROXMOX', '23:00'),
    },
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
