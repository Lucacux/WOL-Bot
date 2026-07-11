"""WOL-Bot — punto de entrada.

Solo arma el cliente de Discord, registra los slash-commands, lanza los
background loops (schedule + failsafe) y corre el bot. Toda la lógica vive en
los módulos: config / network / embeds / monitors / scheduler / views.
"""
import sys
import asyncio

import discord

import config
from embeds import build_panel_embed, build_control_embed, build_schedule_embed
from views import (
    WOLPanel,
    SchedulePanel,
    RebootPanel,
    ShutdownPanel,
    gather_statuses,
)
from scheduler import schedule_loop, failsafe_loop, load_schedule


intents = discord.Intents.default()
intents.message_content = True
bot  = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(bot)


@bot.event
async def on_ready():
    print(f"WOL Bot ONLINE: {bot.user}")
    await tree.sync()
    asyncio.create_task(schedule_loop(bot))
    asyncio.create_task(failsafe_loop(bot))

    channel = bot.get_channel(config.CHANNEL_ID)
    if not channel:
        return

    statuses = await gather_statuses()
    await channel.send(embed=build_panel_embed(statuses), view=WOLPanel(statuses))


@tree.command(name="wol", description="Abre el panel Wake-on-LAN")
async def wol_command(interaction: discord.Interaction):
    statuses = await gather_statuses()
    await interaction.response.send_message(
        embed=build_panel_embed(statuses), view=WOLPanel(statuses)
    )


@tree.command(name="schedule", description="Gestionar el horario automático de un servidor")
async def schedule_command(interaction: discord.Interaction):
    default_key = next(iter(config.SERVERS))
    cfg = load_schedule(default_key)
    await interaction.response.send_message(
        embed=build_schedule_embed(default_key, cfg),
        view=SchedulePanel(default_key, cfg),
    )


@tree.command(name="reboot", description="Reiniciar uno de los servidores (vía SSH)")
async def reboot_command(interaction: discord.Interaction):
    statuses = await gather_statuses()
    embed = build_control_embed(
        "🔄  Reboot Control",
        "Elegí el servidor a reiniciar. Solo se pueden reiniciar los ONLINE.",
        statuses,
    )
    await interaction.response.send_message(embed=embed, view=RebootPanel(statuses))


@tree.command(name="shutdown", description="Apagar uno de los servidores (vía SSH)")
async def shutdown_command(interaction: discord.Interaction):
    statuses = await gather_statuses()
    embed = build_control_embed(
        "🌙  Shutdown Control",
        "Elegí el servidor a apagar. Solo se pueden apagar los ONLINE.",
        statuses,
    )
    await interaction.response.send_message(embed=embed, view=ShutdownPanel(statuses))


if __name__ == "__main__":
    if not config.TOKEN:
        print("ERROR: Falta DISCORD_TOKEN en .env")
        sys.exit(1)
    bot.run(config.TOKEN)
