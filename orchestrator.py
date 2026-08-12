"""Primitivas de encendido y apagado reutilizables por automatizaciones externas."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime

import config
from maintenance import active_lease
from network import check_status, ssh_shutdown, wake
from schedule_store import should_be_online


@dataclass(frozen=True)
class EnsureOnlineResult:
    server_key: str
    ready: bool
    already_online: bool
    wol_attempts: int
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PowerRestoreResult:
    server_key: str
    ok: bool
    action: str          # shutdown | kept-online | already-offline | blocked | error
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


async def ensure_online(
    server_key: str,
    *,
    attempts: int = 3,
    attempt_timeout: int = 180,
    poll_seconds: int = 10,
    boot_grace: int = 90,
) -> EnsureOnlineResult:
    """Asegura que un servidor responda, reintentando WOL si hace falta.

    Tras la primera respuesta se aplica una gracia fija y se vuelve a comprobar:
    un ping temprano del firmware o del kernel no equivale a un SO listo.
    Updates-Bot hace después su propia prueba de conexión Ansible.
    """
    if server_key not in config.SERVERS:
        return EnsureOnlineResult(server_key, False, False, 0, "servidor desconocido")
    if attempts < 1:
        return EnsureOnlineResult(server_key, False, False, 0, "sin intentos configurados")
    if attempt_timeout < 0 or poll_seconds < 1 or boot_grace < 0:
        return EnsureOnlineResult(server_key, False, False, 0, "parámetros de espera inválidos")

    ip = config.SERVERS[server_key]["ip"]
    if await check_status(ip):
        return EnsureOnlineResult(server_key, True, True, 0, "ya estaba online")

    loop = asyncio.get_running_loop()
    sent = 0
    for attempt in range(1, attempts + 1):
        if not await wake(server_key):
            return EnsureOnlineResult(
                server_key, False, False, sent, "no se pudo enviar el magic packet"
            )
        sent = attempt
        deadline = loop.time() + attempt_timeout

        while True:
            if await check_status(ip):
                if boot_grace:
                    await asyncio.sleep(boot_grace)
                if await check_status(ip):
                    return EnsureOnlineResult(
                        server_key, True, False, sent, "encendido por WOL"
                    )
                break

            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_seconds, remaining))

    return EnsureOnlineResult(
        server_key,
        False,
        False,
        sent,
        f"sin respuesta después de {sent} intentos WOL",
    )


async def restore_power_state(
    server_key: str,
    *,
    owner: str = "",
    now: datetime | None = None,
) -> PowerRestoreResult:
    """Devuelve a OFF un servidor que una automatización encendió.

    Pensado para el caso "lo prendí a las 3 AM solo para hacerle el backup":
    quien lo encendió llama a esto al terminar y el servidor vuelve a como
    estaba. **Solo hay que llamarlo para servidores que uno encendió**: esta
    función no sabe quién prendió qué, únicamente decide si es seguro apagar.

    No apaga si:
      · el server ya está offline (no hay nada que restaurar);
      · el horario dice que ahora tendría que estar encendido — ahí lo dejó
        prendido el scheduler, no nosotros, y apagarlo sería una sorpresa;
      · hay una reserva de mantenimiento de OTRO owner: alguien más está
        trabajando sobre ese server justo ahora.
    """
    if server_key not in config.SERVERS:
        return PowerRestoreResult(server_key, False, "error", "servidor desconocido")

    lease = active_lease(server_key)
    if lease and lease.owner != owner:
        return PowerRestoreResult(
            server_key,
            True,
            "blocked",
            f"reservado por {lease.owner} ({lease.remaining_seconds}s restantes)",
        )

    if not await check_status(config.SERVERS[server_key]["ip"]):
        return PowerRestoreResult(server_key, True, "already-offline", "ya estaba apagado")

    online_expected, reason = should_be_online(server_key, now=now)
    if online_expected:
        return PowerRestoreResult(server_key, True, "kept-online", reason)

    if not await ssh_shutdown(server_key):
        return PowerRestoreResult(
            server_key, False, "error", f"{reason}, pero el apagado por SSH falló"
        )
    return PowerRestoreResult(server_key, True, "shutdown", f"apagado: {reason}")
