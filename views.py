"""Todas las Views y Modals de discord.ui.

Los paneles (WOL / reboot / shutdown) construyen sus botones iterando sobre
`config.SERVERS`, con un callback por servidor generado por un factory. El panel
de horario incluye un Select para elegir a qué servidor aplica la config, así el
mismo panel sirve para NAS, Media o cualquier server que agregues.
"""
from datetime import datetime

import asyncio

import discord

import config
from network import check_status, wake, ssh_reboot, ssh_shutdown
from embeds import (
    build_panel_embed,
    build_control_embed,
    build_schedule_embed,
)
from monitors import monitor_boot, monitor_reboot, monitor_shutdown
from scheduler import (
    load_schedule,
    save_schedule,
    parse_hhmm,
    in_uptime_window,
)


# ──────────────────────────────────────────
# Cancelar apagado programado
# ──────────────────────────────────────────
class CancelShutdownView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=config.SHUTDOWN_WARN_SECS + 15)
        self.cancelled = False
        btn = discord.ui.Button(label="🚫  No apagar", style=discord.ButtonStyle.danger)
        btn.callback = self._cancel
        self.add_item(btn)

    async def _cancel(self, interaction: discord.Interaction):
        self.cancelled = True
        self.stop()
        await interaction.response.defer()


# ──────────────────────────────────────────
# Modal — editar horas
# ──────────────────────────────────────────
class TimeInputModal(discord.ui.Modal):
    def __init__(self, server_key: str, field: str, current_val: str):
        label = "encendido" if field == "wake_time" else "apagado"
        super().__init__(title=f"Cambiar hora de {label}")
        self.server_key = server_key
        self.field = field
        self.time_input = discord.ui.TextInput(
            label="Hora (formato HH:MM)",
            placeholder="Ej: 06:30",
            default=current_val,
            max_length=5,
            min_length=4,
        )
        self.add_item(self.time_input)

    async def on_submit(self, interaction: discord.Interaction):
        val = self.time_input.value.strip()
        try:
            parse_hhmm(val)
        except Exception:
            await interaction.response.send_message(
                "❌ Formato inválido. Usá `HH:MM` — por ejemplo `06:30`.",
                ephemeral=True,
            )
            return

        cfg = load_schedule(self.server_key)
        cfg[self.field] = val
        if self.field == "wake_time":
            cfg["last_wake_date"] = None
        else:
            cfg["last_shutdown_date"]      = None
            cfg["shutdown_cancelled_date"] = None
        save_schedule(self.server_key, cfg)

        label = "encendido" if self.field == "wake_time" else "apagado"
        await interaction.response.send_message(
            f"✅ Hora de **{label}** de **{config.SERVERS[self.server_key]['name']}** "
            f"actualizada a `{val}`.",
            ephemeral=True,
        )


# ──────────────────────────────────────────
# Panel de horario (con selector de servidor)
# ──────────────────────────────────────────
class SchedulePanel(discord.ui.View):
    def __init__(self, server_key: str, cfg: dict):
        super().__init__(timeout=300)
        self.server_key = server_key
        self.cfg = cfg
        self._build()

    def _build(self):
        self.clear_items()

        # row 0 — selector de servidor (solo si hay más de uno)
        if len(config.SERVERS) > 1:
            select = discord.ui.Select(
                placeholder="Elegí el servidor…",
                options=[
                    discord.SelectOption(
                        label=srv["name"],
                        value=key,
                        emoji=srv["emoji"],
                        default=(key == self.server_key),
                    )
                    for key, srv in config.SERVERS.items()
                ],
                row=0,
            )
            select.callback = self._on_select
            self.add_item(select)

        enabled = self.cfg["enabled"]
        toggle = discord.ui.Button(
            label="⏸️  Pausar horario" if enabled else "▶️  Activar horario",
            style=discord.ButtonStyle.danger if enabled else discord.ButtonStyle.success,
            row=1,
        )
        toggle.callback = self._toggle
        self.add_item(toggle)

        btn_wake = discord.ui.Button(label="⏰  Cambiar encendido", style=discord.ButtonStyle.primary, row=2)
        btn_wake.callback = self._set_wake
        self.add_item(btn_wake)

        btn_shut = discord.ui.Button(label="🌙  Cambiar apagado", style=discord.ButtonStyle.primary, row=2)
        btn_shut.callback = self._set_shutdown
        self.add_item(btn_shut)

        fs_enabled = self.cfg.get("failsafe_enabled", True)
        btn_failsafe = discord.ui.Button(
            label="🛟  Pausar failsafe" if fs_enabled else "🛟  Activar failsafe",
            style=discord.ButtonStyle.danger if fs_enabled else discord.ButtonStyle.success,
            row=3,
        )
        btn_failsafe.callback = self._toggle_failsafe
        self.add_item(btn_failsafe)

        btn_reset = discord.ui.Button(label="🔄  Resetear estado de hoy", style=discord.ButtonStyle.secondary, row=4)
        btn_reset.callback = self._reset_today
        self.add_item(btn_reset)

    async def _refresh(self, interaction: discord.Interaction):
        self.cfg = load_schedule(self.server_key)
        self._build()
        await interaction.response.edit_message(embed=build_schedule_embed(self.server_key, self.cfg), view=self)

    async def _on_select(self, interaction: discord.Interaction):
        self.server_key = interaction.data["values"][0]
        await self._refresh(interaction)

    async def _toggle(self, interaction: discord.Interaction):
        cfg = load_schedule(self.server_key)
        cfg["enabled"] = not cfg["enabled"]
        save_schedule(self.server_key, cfg)
        await self._refresh(interaction)

    async def _toggle_failsafe(self, interaction: discord.Interaction):
        cfg = load_schedule(self.server_key)
        cfg["failsafe_enabled"] = not cfg.get("failsafe_enabled", True)
        save_schedule(self.server_key, cfg)
        await self._refresh(interaction)

    async def _set_wake(self, interaction: discord.Interaction):
        cfg = load_schedule(self.server_key)
        await interaction.response.send_modal(TimeInputModal(self.server_key, "wake_time", cfg["wake_time"]))

    async def _set_shutdown(self, interaction: discord.Interaction):
        cfg = load_schedule(self.server_key)
        await interaction.response.send_modal(TimeInputModal(self.server_key, "shutdown_time", cfg["shutdown_time"]))

    async def _reset_today(self, interaction: discord.Interaction):
        cfg = load_schedule(self.server_key)
        cfg["last_wake_date"]          = None
        cfg["last_shutdown_date"]      = None
        cfg["shutdown_cancelled_date"] = None
        save_schedule(self.server_key, cfg)
        await self._refresh(interaction)


# ──────────────────────────────────────────
# Reboot — confirmación + panel
# ──────────────────────────────────────────
class RebootConfirmView(discord.ui.View):
    def __init__(self, server_key: str):
        super().__init__(timeout=30)
        self.server_key = server_key

        confirm = discord.ui.Button(label="✅  Confirmar reinicio", style=discord.ButtonStyle.danger)
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction):
        srv = config.SERVERS[self.server_key]

        if not await check_status(srv["ip"]):
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"ℹ️  {srv['name']} está OFFLINE",
                    description="No se puede reiniciar un servidor apagado. Usá el panel WOL para despertarlo.",
                    color=0x95a5a6,
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🔄  Enviando reinicio → {srv['name']}",
                description="Comando `sudo shutdown -r now` vía SSH...",
                color=0xe67e22,
                timestamp=datetime.now(),
            ),
            view=None,
        )

        if not await ssh_reboot(self.server_key):
            await interaction.edit_original_response(embed=discord.Embed(
                title="❌  Error al reiniciar",
                description="No se pudo conectar por SSH. Verificá clave, puerto y permiso de sudo.",
                color=0xff0000,
                timestamp=datetime.now(),
            ))
            return

        await interaction.edit_original_response(embed=discord.Embed(
            title=f"📡  Reinicio enviado → {srv['name']}",
            description="Monitoreando el ciclo de reinicio abajo 👇",
            color=0x2ecc71,
            timestamp=datetime.now(),
        ))

        monitor_msg = await interaction.channel.send(embed=discord.Embed(
            title=f"🔄  Reiniciando: {srv['name']}",
            color=0xe67e22,
            timestamp=datetime.now(),
        ))
        asyncio.create_task(monitor_reboot(monitor_msg, self.server_key))

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(title="✖️  Reinicio cancelado", color=0x95a5a6),
            view=None,
        )


class RebootPanel(discord.ui.View):
    def __init__(self, statuses: dict):
        super().__init__(timeout=300)
        self.statuses = statuses
        self._build()

    def _build(self):
        self.clear_items()
        for key, srv in config.SERVERS.items():
            online = self.statuses.get(key, False)
            btn = discord.ui.Button(
                label=f"Reiniciar {srv['name']}" if online else f"{srv['name']} OFFLINE",
                style=discord.ButtonStyle.danger if online else discord.ButtonStyle.secondary,
                emoji=srv["emoji"],
                disabled=not online,
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, server_key: str):
        async def _cb(interaction: discord.Interaction):
            srv = config.SERVERS[server_key]
            embed = discord.Embed(
                title=f"⚠️  ¿Reiniciar {srv['name']}?",
                description=(
                    f"Vas a enviar `sudo shutdown -r now` a **{srv['name']}** (`{srv['ip']}`).\n"
                    f"Se van a cortar los servicios mientras reinicia."
                ),
                color=0xe67e22,
            )
            await interaction.response.send_message(
                embed=embed, view=RebootConfirmView(server_key), ephemeral=True
            )
        return _cb


# ──────────────────────────────────────────
# Shutdown — confirmación + panel
# ──────────────────────────────────────────
class ShutdownConfirmView(discord.ui.View):
    def __init__(self, server_key: str):
        super().__init__(timeout=30)
        self.server_key = server_key

        confirm = discord.ui.Button(label="✅  Confirmar apagado", style=discord.ButtonStyle.danger)
        confirm.callback = self._confirm
        self.add_item(confirm)

        cancel = discord.ui.Button(label="Cancelar", style=discord.ButtonStyle.secondary)
        cancel.callback = self._cancel
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction):
        srv = config.SERVERS[self.server_key]

        if not await check_status(srv["ip"]):
            await interaction.response.edit_message(
                embed=discord.Embed(
                    title=f"ℹ️  {srv['name']} ya está OFFLINE",
                    description="El servidor ya está apagado.",
                    color=0x95a5a6,
                ),
                view=None,
            )
            return

        await interaction.response.edit_message(
            embed=discord.Embed(
                title=f"🌙  Enviando apagado → {srv['name']}",
                description="Comando `sudo shutdown -h now` vía SSH...",
                color=0xe67e22,
                timestamp=datetime.now(),
            ),
            view=None,
        )

        if not await ssh_shutdown(self.server_key):
            await interaction.edit_original_response(embed=discord.Embed(
                title="❌  Error al apagar",
                description="No se pudo conectar por SSH. Verificá clave, puerto y permiso de sudo.",
                color=0xff0000,
                timestamp=datetime.now(),
            ))
            return

        await interaction.edit_original_response(embed=discord.Embed(
            title=f"📡  Apagado enviado → {srv['name']}",
            description="Monitoreando la desconexión abajo 👇",
            color=0x2ecc71,
            timestamp=datetime.now(),
        ))

        monitor_msg = await interaction.channel.send(embed=discord.Embed(
            title=f"🌙  Apagando: {srv['name']}",
            color=0xe67e22,
            timestamp=datetime.now(),
        ))
        asyncio.create_task(monitor_shutdown(monitor_msg, self.server_key))

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            embed=discord.Embed(title="✖️  Apagado cancelado", color=0x95a5a6),
            view=None,
        )


class ShutdownPanel(discord.ui.View):
    def __init__(self, statuses: dict):
        super().__init__(timeout=300)
        self.statuses = statuses
        self._build()

    def _build(self):
        self.clear_items()
        for key, srv in config.SERVERS.items():
            online = self.statuses.get(key, False)
            btn = discord.ui.Button(
                label=f"Apagar {srv['name']}" if online else f"{srv['name']} OFFLINE",
                style=discord.ButtonStyle.danger if online else discord.ButtonStyle.secondary,
                emoji=srv["emoji"],
                disabled=not online,
            )
            btn.callback = self._make_callback(key)
            self.add_item(btn)

    def _make_callback(self, server_key: str):
        async def _cb(interaction: discord.Interaction):
            srv = config.SERVERS[server_key]

            # Aviso si el failsafe de ESTE server podría reencenderlo.
            note = ""
            cfg = load_schedule(server_key)
            would_rewake = (
                cfg.get("enabled")
                and cfg.get("failsafe_enabled", True)
                and in_uptime_window(
                    datetime.now().time(),
                    parse_hhmm(cfg["wake_time"]),
                    parse_hhmm(cfg["shutdown_time"]),
                )
            )
            if would_rewake:
                note = (
                    "\n\n⚠️  El *failsafe* de este servidor está activo y estás dentro de su "
                    "franja horaria: podría reencenderlo. Pausalo desde `/schedule` si querés "
                    "que quede apagado."
                )

            embed = discord.Embed(
                title=f"⚠️  ¿Apagar {srv['name']}?",
                description=(
                    f"Vas a enviar `sudo shutdown -h now` a **{srv['name']}** (`{srv['ip']}`).\n"
                    f"Se van a cortar sus servicios hasta que lo despiertes con WOL."
                    f"{note}"
                ),
                color=0xe67e22,
            )
            await interaction.response.send_message(
                embed=embed, view=ShutdownConfirmView(server_key), ephemeral=True
            )
        return _cb


# ──────────────────────────────────────────
# Panel WOL principal
# ──────────────────────────────────────────
class WOLPanel(discord.ui.View):
    def __init__(self, statuses: dict):
        super().__init__(timeout=300)
        self.statuses = statuses
        self._build()

    def _build(self):
        self.clear_items()
        for key, srv in config.SERVERS.items():
            online = self.statuses.get(key, False)
            btn = discord.ui.Button(
                label=f"Despertar {srv['name']}" if not online else f"{srv['name']} ya está ONLINE",
                style=discord.ButtonStyle.success if not online else discord.ButtonStyle.secondary,
                emoji=srv["emoji"],
                custom_id=f"wake_{key}",
                disabled=online,
            )
            btn.callback = self._make_wake_callback(key)
            self.add_item(btn)

        refresh_btn = discord.ui.Button(
            label="Actualizar estado",
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            custom_id="refresh",
        )
        refresh_btn.callback = self._refresh_status
        self.add_item(refresh_btn)

    def _make_wake_callback(self, server_key: str):
        async def _cb(interaction: discord.Interaction):
            srv = config.SERVERS[server_key]

            if await check_status(srv["ip"]):
                await interaction.response.send_message(
                    f"ℹ️ `{srv['name']}` ya está ONLINE.", ephemeral=True
                )
                return

            if not await wake(server_key):
                await interaction.response.send_message(
                    "❌ No se encontró `wakeonlan`.\nInstalá: `sudo pacman -S wakeonlan`",
                    ephemeral=True,
                )
                return

            embed = discord.Embed(
                title=f"📡  Magic packet enviado → {srv['name']}",
                description=f"MAC: `{srv['mac']}`",
                color=0x3498db,
                timestamp=datetime.now(),
            )
            embed.set_footer(text="Monitoreando arranque...")

            await interaction.response.send_message(
                f"⚡ Despertando **{srv['name']}**...", ephemeral=True
            )
            monitor_msg = await interaction.channel.send(embed=embed)
            asyncio.create_task(monitor_boot(monitor_msg, server_key))
        return _cb

    async def _refresh_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        self.statuses = await gather_statuses()
        self._build()
        await interaction.message.edit(embed=build_panel_embed(self.statuses), view=self)


# ──────────────────────────────────────────
# Helper — estado de todos los servidores a la vez
# ──────────────────────────────────────────
async def gather_statuses() -> dict:
    keys    = list(config.SERVERS.keys())
    results = await asyncio.gather(*(check_status(config.SERVERS[k]["ip"]) for k in keys))
    return dict(zip(keys, results))
