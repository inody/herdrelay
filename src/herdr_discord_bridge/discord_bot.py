from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .approval import ApprovalError, apply_approval, strategy_for
from .config import AppConfig
from .formatter import format_bindings, format_status, format_tail, truncate
from .herdr_client import HerdrClient, TargetResolutionError
from .models import AuditEntry
from .security import DiscordLocation, SecurityError, SecurityPolicy
from .store import Store

LOG = logging.getLogger(__name__)


class HerdrDiscordBot(commands.Bot):
    def __init__(self, *, config: AppConfig, store: Store, client: HerdrClient):
        intents = discord.Intents.default()
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.store = store
        self.client = client
        self.security = SecurityPolicy(config)

    async def setup_hook(self) -> None:
        await self.add_cog(HerdrCog(self))
        if self.config.allowed_guild_ids:
            for guild_id in self.config.allowed_guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
                LOG.info("Synced commands to guild %s", guild_id)
        else:
            await self.tree.sync()
            LOG.info("Synced global commands")


class HerdrCog(commands.Cog):
    herdr = app_commands.Group(name="herdr", description="Monitor and control Herdr panes")

    def __init__(self, bot: HerdrDiscordBot):
        self.bot = bot

    @herdr.command(name="status", description="List visible Herdr agents and panes")
    async def status(self, interaction: discord.Interaction) -> None:
        try:
            self.bot.security.ensure_read_allowed(_location(interaction))
            await interaction.response.defer(thinking=True)
            targets = self.bot.client.list_targets()
            await interaction.followup.send(
                format_status(targets, max_chars=self.bot.config.max_output_chars)
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @herdr.command(name="ids", description="Show Discord IDs for this location")
    async def ids(self, interaction: discord.Interaction) -> None:
        location = _location(interaction)
        message = (
            "```text\n"
            f"user_id:    {interaction.user.id}\n"
            f"guild_id:   {interaction.guild_id}\n"
            f"channel_id: {location.channel_id}\n"
            f"thread_id:  {location.thread_id or '-'}\n"
            "```"
        )
        await interaction.response.send_message(message, ephemeral=True)

    @herdr.command(name="tail", description="Show recent output for a bound or explicit target")
    @app_commands.describe(target="Herdr pane or agent target", lines="Number of recent lines")
    async def tail(
        self,
        interaction: discord.Interaction,
        target: str | None = None,
        lines: int | None = None,
    ) -> None:
        try:
            location = _location(interaction)
            self.bot.security.ensure_read_allowed(location)
            await interaction.response.defer(thinking=True)
            resolved = self._target_from_arg(location, target)
            output = self.bot.client.read(
                resolved,
                lines=_clamp_lines(lines, self.bot.config.max_tail_lines),
            )
            await interaction.followup.send(
                format_tail(output, max_chars=self.bot.config.max_output_chars)
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @herdr.command(name="bind", description="Bind this Discord thread or channel to a Herdr target")
    @app_commands.describe(target="Herdr pane or agent target", label="Optional label")
    async def bind(
        self,
        interaction: discord.Interaction,
        target: str,
        label: str | None = None,
    ) -> None:
        try:
            location = _location(interaction)
            self.bot.security.ensure_read_allowed(location)
            await interaction.response.defer(thinking=True)
            resolved = self.bot.client.resolve_target(target).target
            self.bot.store.upsert_binding(
                guild_id=location.guild_id or 0,
                channel_id=location.channel_id,
                thread_id=location.thread_id,
                herdr_target=resolved,
                label=label,
                created_by=interaction.user.id,
            )
            output = self.bot.client.read(resolved, lines=min(20, self.bot.config.max_tail_lines))
            message = f"Bound this location to `{resolved}`."
            if label:
                message += f" Label: `{label}`."
            await interaction.followup.send(
                message + "\n" + format_tail(output, max_chars=self.bot.config.max_output_chars)
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @herdr.command(name="bindings", description="Show Herdr bindings for this Discord server")
    async def bindings(self, interaction: discord.Interaction) -> None:
        try:
            location = _location(interaction)
            self.bot.security.ensure_read_allowed(location)
            bindings = self.bot.store.list_bindings(guild_id=location.guild_id)
            await interaction.response.send_message(
                format_bindings(bindings, max_chars=self.bot.config.max_output_chars)
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @herdr.command(name="unbind", description="Remove the binding for this thread or channel")
    async def unbind(self, interaction: discord.Interaction) -> None:
        try:
            location = _location(interaction)
            self.bot.security.ensure_read_allowed(location)
            count = self.bot.store.delete_binding(
                guild_id=location.guild_id or 0,
                channel_id=location.channel_id,
                thread_id=location.thread_id,
            )
            await interaction.response.send_message(
                "Removed binding." if count else "No binding found for this location."
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @herdr.command(name="send", description="Send text to a bound or explicit Herdr target")
    @app_commands.describe(target="Herdr pane or agent target", message="Message to send")
    async def send(
        self,
        interaction: discord.Interaction,
        message: str,
        target: str | None = None,
    ) -> None:
        location = _location(interaction)
        resolved = target
        try:
            resolved = self._target_from_arg(location, target)
            self.bot.security.ensure_send_allowed(interaction.user.id, location, message)
            await interaction.response.defer(thinking=True)
            self.bot.client.send(resolved, message)
            self._audit(interaction, location, "send", resolved, message, "ok")
            output = self.bot.client.read(resolved, lines=40)
            await interaction.followup.send(
                f"Sent to `{resolved}`.\n"
                + format_tail(output, max_chars=self.bot.config.max_output_chars)
            )
        except Exception as exc:
            self._audit(interaction, location, "send", resolved, message, f"error: {exc}")
            await _send_error(interaction, exc)

    @herdr.command(name="approve", description="Preview recent output, then approve via button")
    @app_commands.describe(target="Herdr pane or agent target")
    async def approve(
        self,
        interaction: discord.Interaction,
        target: str | None = None,
    ) -> None:
        location = _location(interaction)
        resolved = target
        try:
            self.bot.security.ensure_approve_allowed(interaction.user.id, location)
            resolved = self._target_from_arg(location, target)
            target_info = self.bot.client.resolve_target(resolved)
            strategy = strategy_for(self.bot.config, target_info.agent_name)
            await interaction.response.defer(thinking=True)
            output = self.bot.client.read(resolved, lines=self.bot.config.max_tail_lines)
            self._audit(interaction, location, "approve_preview", resolved, None, "ok")
            view = ApprovalView(
                bot=self.bot,
                user_id=interaction.user.id,
                location=location,
                target=resolved,
                strategy=strategy,
            )
            await interaction.followup.send(
                "Review the recent output, then choose Approve or Cancel.\n"
                + format_tail(output, max_chars=self.bot.config.max_output_chars),
                view=view,
            )
        except Exception as exc:
            self._audit(interaction, location, "approve_preview", resolved, None, f"error: {exc}")
            await _send_error(interaction, exc)

    def _target_from_arg(self, location: DiscordLocation, target: str | None) -> str:
        if target:
            return self.bot.client.resolve_target(target).target
        binding = self.bot.store.get_binding(
            guild_id=location.guild_id or 0,
            channel_id=location.channel_id,
            thread_id=location.thread_id,
        )
        if not binding:
            raise TargetResolutionError("No target provided and this location is not bound.")
        return binding.herdr_target

    def _audit(
        self,
        interaction: discord.Interaction,
        location: DiscordLocation,
        action: str,
        target: str | None,
        payload: str | None,
        result: str,
    ) -> None:
        self.bot.store.add_audit(
            AuditEntry(
                discord_user_id=str(interaction.user.id),
                guild_id=str(location.guild_id) if location.guild_id is not None else None,
                channel_id=str(location.channel_id),
                thread_id=str(location.thread_id) if location.thread_id is not None else None,
                action=action,
                herdr_target=target,
                payload_preview=truncate(payload or "", max_chars=200) if payload else None,
                result=truncate(result, max_chars=500),
            )
        )


class ApprovalView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: HerdrDiscordBot,
        user_id: int,
        location: DiscordLocation,
        target: str,
        strategy,
    ):
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = user_id
        self.location = location
        self.target = target
        self.strategy = strategy

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.danger)
    async def approve_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the requesting user can approve.", ephemeral=True)
            return
        try:
            self.bot.security.ensure_approve_allowed(interaction.user.id, self.location)
            apply_approval(self.bot.client, self.target, self.strategy)
            self.bot.store.add_audit(
                AuditEntry(
                    discord_user_id=str(interaction.user.id),
                    guild_id=str(self.location.guild_id) if self.location.guild_id is not None else None,
                    channel_id=str(self.location.channel_id),
                    thread_id=str(self.location.thread_id) if self.location.thread_id else None,
                    action="approve",
                    herdr_target=self.target,
                    result="ok",
                )
            )
            self._disable()
            await interaction.response.edit_message(content=f"Approved `{self.target}`.", view=self)
        except Exception as exc:
            await _send_error(interaction, exc)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the requesting user can cancel.", ephemeral=True)
            return
        self._disable()
        await interaction.response.edit_message(content=f"Cancelled approval for `{self.target}`.", view=self)

    async def on_timeout(self) -> None:
        self._disable()

    def _disable(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True


def _location(interaction: discord.Interaction) -> DiscordLocation:
    channel = interaction.channel
    parent_id = getattr(channel, "parent_id", None)
    channel_id = parent_id or interaction.channel_id
    thread_id = interaction.channel_id if parent_id else None
    return DiscordLocation(
        guild_id=interaction.guild_id,
        channel_id=int(channel_id),
        thread_id=int(thread_id) if thread_id else None,
    )


def _clamp_lines(lines: int | None, maximum: int) -> int:
    if lines is None:
        return maximum
    return max(1, min(lines, maximum))


async def _send_error(interaction: discord.Interaction, exc: Exception) -> None:
    LOG.exception("Discord command failed")
    if isinstance(exc, (SecurityError, TargetResolutionError, ApprovalError)):
        message = str(exc)
    else:
        message = f"Command failed: {exc}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def build_bot(config: AppConfig, store: Store, client: HerdrClient) -> HerdrDiscordBot:
    return HerdrDiscordBot(config=config, store=store, client=client)
