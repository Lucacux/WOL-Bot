"""I/O de red: ping, Wake-on-LAN y ejecución remota por SSH.

Todo lo que toca la red vive acá y no sabe nada de Discord. Las funciones
bloqueantes (`ping`, `send_wol`) se envuelven con `asyncio.to_thread` desde los
wrappers async para no frenar el event loop.
"""
import os
import asyncio
import shutil
import subprocess

import config


# ──────────────────────────────────────────
# PING
# ──────────────────────────────────────────
def ping(ip: str) -> bool:
    result = subprocess.run(
        ["ping", "-c", "1", "-W", "1", ip],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


async def check_status(ip: str) -> bool:
    return await asyncio.to_thread(ping, ip)


async def tcp_probe(ip: str, port: int, timeout: int | None = None) -> bool:
    """¿Acepta conexión TCP en `port`? Sonda para servers en otra VLAN.

    El ICMP entre VLANs lo descarta el gateway, así que un ping da siempre
    "caído". Un connect además prueba algo más fuerte que el ping: que el
    servicio levantó, no solo que el kernel contesta.
    """
    timeout = config.PROBE_TCP_TIMEOUT if timeout is None else timeout
    writer = None
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def probe(server_key: str) -> bool:
    """Estado de UN servidor, con el método que ese servidor tenga configurado.

    Es el único punto por el que debería pasar una comprobación de estado: cada
    servidor sabe si se lo sondea por ICMP o por TCP, y quien pregunta no.
    """
    srv = config.SERVERS[server_key]
    if srv.get("probe") == "tcp":
        return await tcp_probe(srv["ip"], int(srv.get("probe_port", 22)))
    return await check_status(srv["ip"])


async def is_server_down(server_key: str) -> bool:
    """Detección de caída con debounce.

    Devuelve True solo tras FAILSAFE_CONFIRM_CHECKS sondas consecutivas
    fallidas. En estado normal (server ONLINE) la primera responde y sale con
    una sola sonda → impacto de red despreciable. Solo cuando está caído escala
    a varias espaciadas para confirmar y evitar falsos positivos.
    """
    for i in range(config.FAILSAFE_CONFIRM_CHECKS):
        if await probe(server_key):
            return False
        if i < config.FAILSAFE_CONFIRM_CHECKS - 1:
            await asyncio.sleep(config.FAILSAFE_CONFIRM_GAP)
    return True


# ──────────────────────────────────────────
# WAKE-ON-LAN
# ──────────────────────────────────────────
def send_wol(mac: str, target: str | None = None, port: int | None = None) -> bool:
    """Manda el magic packet. Sin `target` va al broadcast (255.255.255.255).

    Con `target` va unicast a esa IP, que es la única forma de que cruce a otra
    VLAN: el broadcast limitado no lo rutea ningún router. La NIC lo reconoce
    igual — el magic packet se detecta por patrón dentro de la trama, no por
    cómo esté direccionada.
    """
    wol_bin = "/usr/bin/wakeonlan"
    if not os.path.exists(wol_bin):
        wol_bin = shutil.which("wakeonlan")
        if not wol_bin:
            return False
    cmd = [wol_bin]
    if target:
        cmd += ["-i", target, "-p", str(port or config.WOL_DEFAULT_PORT)]
    cmd.append(mac)
    r = subprocess.run(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


async def wake(server_key: str) -> bool:
    srv = config.SERVERS[server_key]
    return await asyncio.to_thread(
        send_wol, srv["mac"], srv.get("wol_target"), srv.get("wol_port")
    )


# ──────────────────────────────────────────
# SSH — apagado / reinicio remoto
# ──────────────────────────────────────────
async def ssh_run(server_key: str, remote_cmd: str) -> bool:
    srv = config.SERVERS[server_key]
    sc  = config.SSH_CONFIG[server_key]
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
    # Por defecto 'shutdown -r now' en vez de 'reboot', para reutilizar el mismo
    # NOPASSWD de sudo que ya está configurado para el apagado. Un servidor que
    # entra por clave de comando forzado manda su propia palabra (ver config).
    return await ssh_run(server_key, config.reboot_cmd(server_key))


async def ssh_shutdown(server_key: str) -> bool:
    return await ssh_run(server_key, config.shutdown_cmd(server_key))
