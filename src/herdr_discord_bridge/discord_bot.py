from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .approval import ApprovalError, apply_approval, ensure_blocked_for_approval, strategy_for
from .config import AppConfig, ApprovalStrategy
from .dashboard import DashboardManager
from .formatter import format_bindings, format_status, format_tail, truncate
from .herdr_client import HerdrClient, TargetResolutionError
from .models import AuditEntry
from .security import DiscordLocation, SecurityError, SecurityPolicy
from .store import Store
from .watcher import EventWatcher

LOG = logging.getLogger(__name__)


class HerdrDiscordBot(commands.Bot):
    def __init__(self, *, config: AppConfig, store: Store, client: HerdrClient):
        intents = discord.Intents.default()
        if config.enable_reply_send:
            intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.store = store
        self.client = client
        self.security = SecurityPolicy(config)
        self._watcher_task: asyncio.Task | None = None
        self._watcher: EventWatcher | None = None
        self._dashboard_task: asyncio.Task | None = None
        self.dashboard: DashboardManager | None = None

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
        if self.config.enable_watcher:
            self._watcher = EventWatcher(
                bot=self,
                config=self.config,
                store=self.store,
                client=self.client,
                approval_view_factory=self._approval_view_for_event,
            )
            self._watcher_task = asyncio.create_task(self._watcher.run_forever())
            LOG.info("Started Herdr event watcher")
        if self.config.enable_dashboard:
            self.dashboard = DashboardManager(
                bot=self,
                config=self.config,
                store=self.store,
                client=self.client,
            )
            self._dashboard_task = asyncio.create_task(self.dashboard.run_forever())
            LOG.info("Started dashboard updater")

    async def close(self) -> None:
        if self._watcher:
            self._watcher.stop()
        if self._watcher_task:
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass
        if self.dashboard:
            self.dashboard.stop()
        if self._dashboard_task:
            self._dashboard_task.cancel()
            try:
                await self._dashboard_task
            except asyncio.CancelledError:
                pass
        await super().close()

    def _approval_view_for_event(self, target: str) -> discord.ui.View | None:
        if not self.config.enable_approve:
            return None
        try:
            target_info = self.client.resolve_target(target)
            strategy = strategy_for(self.config, target_info.agent_name)
        except Exception:
            LOG.exception("Cannot create approval view for watcher event")
            return None
        return ApprovalView(
            bot=self,
            user_id=None,
            location=None,
            target=target,
            strategy=strategy,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await self._handle_reply_send(message)
        await self.process_commands(message)

    async def _handle_reply_send(self, message: discord.Message) -> None:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return
        target = self.store.get_notification_target(reference.message_id)
        if target is None:
            return

        content = (message.content or "").strip()
        location = _location_from_message(message)
        try:
            if not content:
                raise SecurityError(
                    "Reply message content is empty. Enable Message Content Intent for this Discord bot."
                )
            self.security.ensure_reply_send_allowed(message.author.id, location, content)
            self.client.send(target, content)
            self.store.add_audit(
                AuditEntry(
                    discord_user_id=str(message.author.id),
                    guild_id=str(location.guild_id) if location.guild_id is not None else None,
                    channel_id=str(location.channel_id),
                    thread_id=str(location.thread_id) if location.thread_id is not None else None,
                    action="reply_send",
                    herdr_target=target,
                    payload_preview=truncate(content, max_chars=200),
                    result="ok",
                )
            )
            await message.reply(f"Sent reply to `{target}`.", mention_author=False)
        except Exception as exc:
            self.store.add_audit(
                AuditEntry(
                    discord_user_id=str(message.author.id),
                    guild_id=str(location.guild_id) if location.guild_id is not None else None,
                    channel_id=str(location.channel_id),
                    thread_id=str(location.thread_id) if location.thread_id is not None else None,
                    action="reply_send",
                    herdr_target=target,
                    payload_preview=truncate(content, max_chars=200) if content else None,
                    result=truncate(f"error: {exc}", max_chars=500),
                )
            )
            await message.reply(str(exc), mention_author=False)


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
            await self._send_target_followup(
                interaction,
                format_tail(output, max_chars=self.bot.config.max_output_chars),
                target=resolved,
                kind="tail",
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
            await self._send_target_followup(
                interaction,
                message + "\n" + format_tail(output, max_chars=self.bot.config.max_output_chars),
                target=resolved,
                kind="bind",
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

    @herdr.command(name="dashboard", description="Refresh or recreate the dashboard message")
    @app_commands.describe(recreate="Create a new dashboard message instead of editing the saved one")
    async def dashboard(self, interaction: discord.Interaction, recreate: bool = False) -> None:
        try:
            location = _location(interaction)
            self.bot.security.ensure_read_allowed(location)
            await interaction.response.defer(thinking=True)
            manager = self.bot.dashboard or DashboardManager(
                bot=self.bot,
                config=self.bot.config,
                store=self.bot.store,
                client=self.bot.client,
            )
            message = await manager.refresh(recreate=recreate)
            await interaction.followup.send(f"Dashboard updated: {message.jump_url}", ephemeral=True)
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
            await self._send_target_followup(
                interaction,
                f"Sent to `{resolved}`.\n"
                + format_tail(output, max_chars=self.bot.config.max_output_chars),
                target=resolved,
                kind="send",
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
            ensure_blocked_for_approval(target_info)
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
            await self._send_target_followup(
                interaction,
                "Review the recent output, then choose Approve or Cancel.\n"
                + format_tail(output, max_chars=self.bot.config.max_output_chars),
                target=resolved,
                kind="approve_preview",
                view=view,
            )
        except Exception as exc:
            self._audit(interaction, location, "approve_preview", resolved, None, f"error: {exc}")
            await _send_error(interaction, exc)

    async def _send_target_followup(
        self,
        interaction: discord.Interaction,
        content: str,
        *,
        target: str,
        kind: str,
        view: discord.ui.View | None = None,
    ) -> discord.WebhookMessage:
        kwargs: dict[str, object] = {"wait": True}
        if view is not None:
            kwargs["view"] = view
        message = await interaction.followup.send(content, **kwargs)
        if self.bot.config.enable_reply_send:
            try:
                self.bot.store.add_notification_message(
                    message_id=message.id,
                    herdr_target=target,
                    kind=kind,
                )
            except Exception:
                LOG.exception("Failed to store reply target for message %s", message.id)
        return message

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
        try:
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
        except Exception:
            LOG.exception("Failed to write audit log")


class ApprovalView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: HerdrDiscordBot,
        user_id: int | None,
        location: DiscordLocation | None,
        target: str,
        strategy: ApprovalStrategy,
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
        if self.user_id is not None and interaction.user.id != self.user_id:
            await interaction.response.send_message("Only the requesting user can approve.", ephemeral=True)
            return
        try:
            if self.location is None:
                self.bot.security.ensure_approve_user_allowed(interaction.user.id)
            else:
                self.bot.security.ensure_approve_allowed(interaction.user.id, self.location)
            target_info = self.bot.client.resolve_target(self.target)
            ensure_blocked_for_approval(target_info)
            apply_approval(self.bot.client, self.target, self.strategy)
            self.bot.store.add_audit(
                AuditEntry(
                    discord_user_id=str(interaction.user.id),
                    guild_id=str(self.location.guild_id)
                    if self.location and self.location.guild_id is not None
                    else None,
                    channel_id=str(self.location.channel_id) if self.location else None,
                    thread_id=str(self.location.thread_id) if self.location and self.location.thread_id else None,
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
        if self.user_id is not None and interaction.user.id != self.user_id:
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


def _location_from_message(message: discord.Message) -> DiscordLocation:
    channel = message.channel
    parent_id = getattr(channel, "parent_id", None)
    channel_id = parent_id or message.channel.id
    thread_id = message.channel.id if parent_id else None
    return DiscordLocation(
        guild_id=message.guild.id if message.guild else None,
        channel_id=int(channel_id),
        thread_id=int(thread_id) if thread_id else None,
    )


def _clamp_lines(lines: int | None, maximum: int) -> int:
    if lines is None:
        return maximum
    return max(1, min(lines, maximum))


async def _send_error(interaction: discord.Interaction, exc: Exception) -> None:
    if isinstance(exc, (SecurityError, TargetResolutionError, ApprovalError)):
        LOG.info("Discord command rejected: %s", exc)
        message = str(exc)
    else:
        LOG.exception("Discord command failed")
        message = f"Command failed: {exc}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def build_bot(config: AppConfig, store: Store, client: HerdrClient) -> HerdrDiscordBot:
    return HerdrDiscordBot(config=config, store=store, client=client)
