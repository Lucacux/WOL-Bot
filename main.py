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


async def _reply_after_probe(interaction: discord.Interaction, build) -> None:
    """Acusa recibo antes de sondear y manda el panel como followup.

    Discord da 3 s para responder una interacción. `gather_statuses()` tarda lo
    que tarde el sondeo más lento: con un server en otra VLAN apagado, el TCP
    probe espera `PROBE_TCP_TIMEOUT` (3 s) y la interacción vence antes de
    responder (`404 Unknown interaction`, "The application did not respond").
    El `defer` corta esa dependencia: el sondeo puede tardar lo que necesite.
    """
    await interaction.response.defer(thinking=True)
    statuses = await gather_statuses()
    embed, view = build(statuses)
    await interaction.followup.send(embed=embed, view=view)


@tree.command(name="wol", description="Abre el panel Wake-on-LAN")
async def wol_command(interaction: discord.Interaction):
    await _reply_after_probe(
        interaction, lambda st: (build_panel_embed(st), WOLPanel(st))
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
    await _reply_after_probe(interaction, lambda st: (
        build_control_embed(
            "🔄  Reboot Control",
            "Elegí el servidor a reiniciar. Solo se pueden reiniciar los ONLINE.",
            st,
        ),
        RebootPanel(st),
    ))


@tree.command(name="shutdown", description="Apagar uno de los servidores (vía SSH)")
async def shutdown_command(interaction: discord.Interaction):
    await _reply_after_probe(interaction, lambda st: (
        build_control_embed(
            "🌙  Shutdown Control",
            "Elegí el servidor a apagar. Solo se pueden apagar los ONLINE.",
            st,
        ),
        ShutdownPanel(st),
    ))


if __name__ == "__main__":
    if not config.TOKEN:
        print("ERROR: Falta DISCORD_TOKEN en .env")
        sys.exit(1)
    bot.run(config.TOKEN)
