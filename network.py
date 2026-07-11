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


async def is_server_down(server_key: str) -> bool:
    """Detección de caída con debounce.

    Devuelve True solo tras FAILSAFE_CONFIRM_CHECKS pings consecutivos fallidos.
    En estado normal (server ONLINE) el primer ping responde y sale con 1 solo
    paquete → impacto de red despreciable. Solo cuando está caído escala a
    varios pings espaciados para confirmar y evitar falsos positivos.
    """
    ip = config.SERVERS[server_key]["ip"]
    for i in range(config.FAILSAFE_CONFIRM_CHECKS):
        if await check_status(ip):
            return False
        if i < config.FAILSAFE_CONFIRM_CHECKS - 1:
            await asyncio.sleep(config.FAILSAFE_CONFIRM_GAP)
    return True


# ──────────────────────────────────────────
# WAKE-ON-LAN
# ──────────────────────────────────────────
def send_wol(mac: str) -> bool:
    wol_bin = "/usr/bin/wakeonlan"
    if not os.path.exists(wol_bin):
        wol_bin = shutil.which("wakeonlan")
        if not wol_bin:
            return False
    r = subprocess.run(
        [wol_bin, mac],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return r.returncode == 0


async def wake(server_key: str) -> bool:
    return await asyncio.to_thread(send_wol, config.SERVERS[server_key]["mac"])


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
    # 'shutdown -r now' en vez de 'reboot' para reutilizar el mismo NOPASSWD de
    # sudo que ya está configurado para el apagado.
    return await ssh_run(server_key, "sudo shutdown -r now")


async def ssh_shutdown(server_key: str) -> bool:
    return await ssh_run(server_key, "sudo shutdown -h now")
