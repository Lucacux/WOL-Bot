"""Monitores de transición de estado tras una acción.

Cada uno recibe un `discord.Message` ya enviado y lo va editando con el progreso
(barra + estado) hasta que la transición se completa o vence el timeout. No
envían mensajes nuevos ni conocen las Views: solo editan lo que se les pasa.
"""
from datetime import datetime

import asyncio

import discord

import config
from network import check_status
from embeds import status_line


async def monitor_boot(message: discord.Message, server_key: str):
    srv     = config.SERVERS[server_key]
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


async def monitor_reboot(message: discord.Message, server_key: str):
    """Dos fases: espera que el server caiga y después que vuelva."""
    srv   = config.SERVERS[server_key]
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


async def monitor_shutdown(message: discord.Message, server_key: str):
    """Una fase: espera que el server deje de responder."""
    srv   = config.SERVERS[server_key]
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
