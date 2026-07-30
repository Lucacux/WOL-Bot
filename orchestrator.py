"""Primitivas de encendido reutilizables por automatizaciones externas."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

import config
from network import check_status, wake


@dataclass(frozen=True)
class EnsureOnlineResult:
    server_key: str
    ready: bool
    already_online: bool
    wol_attempts: int
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
