from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .approval import (
    ApprovalError,
    apply_action,
    deny_strategy_for,
    ensure_blocked_for_approval,
    stop_strategy_for,
    strategy_for,
)
from .config import AppConfig, ApprovalStrategy
from .formatter import (
    format_bindings,
    format_status,
    format_target_card,
    format_tail,
    target_alias,
    truncate,
)
from .herdr_client import HerdrClient, TargetResolutionError
from .models import AuditEntry, HerdrTarget
from .security import DiscordLocation, SecurityError, SecurityPolicy
from .store import Store
from .streams import StreamManager
from .threads import ThreadManager
from .watcher import EventWatcher

LOG = logging.getLogger(__name__)


class HerdrDiscordBot(commands.Bot):
    def __init__(self, *, config: AppConfig, store: Store, client: HerdrClient):
        intents = discord.Intents.default()
        if config.enable_send:
            intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.store = store
        self.client = client
        self.security = SecurityPolicy(config)
        self._watcher_task: asyncio.Task | None = None
        self._watcher: EventWatcher | None = None
        self.thread_manager: ThreadManager | None = None
        self._thread_task: asyncio.Task | None = None
        self.stream_manager: StreamManager | None = None
        self._stream_task: asyncio.Task | None = None

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
                card_view_factory=self._card_view_for_event,
            )
            self._watcher_task = asyncio.create_task(self._watcher.run_forever())
            LOG.info("Started Herdr event watcher")
        if self.config.enable_auto_threads:
            self.thread_manager = ThreadManager(
                bot=self,
                config=self.config,
                store=self.store,
                client=self.client,
            )
            self._thread_task = asyncio.create_task(self.thread_manager.run_forever())
            LOG.info("Started auto thread manager")
        if self.config.enable_streaming:
            self.stream_manager = StreamManager(
                bot=self,
                config=self.config,
                store=self.store,
                client=self.client,
            )
            self._stream_task = asyncio.create_task(self.stream_manager.run_forever())
            LOG.info("Started pane streamer")

    async def close(self) -> None:
        if self._watcher:
            self._watcher.stop()
        if self._watcher_task:
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass
        if self.thread_manager:
            self.thread_manager.stop()
        if self._thread_task:
            self._thread_task.cancel()
            try:
                await self._thread_task
            except asyncio.CancelledError:
                pass
        if self.stream_manager:
            self.stream_manager.stop()
        if self._stream_task:
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
        await super().close()

    def _card_view_for_event(self, target: str) -> discord.ui.View | None:
        try:
            target_info = self.client.resolve_target(target)
        except Exception:
            LOG.exception("Cannot create card view for watcher event")
            return None
        return build_target_card_view(
            bot=self,
            user_id=None,
            location=None,
            target_str=target,
            target_info=target_info,
        )

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await self._handle_thread_send(message)
        await self.process_commands(message)

    async def _handle_thread_send(self, message: discord.Message) -> bool:
        """Forward a normal message posted in a bound thread to its Herdr pane."""
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id is None:
            return False
        location = _location_from_message(message)
        binding = self.store.get_binding(
            guild_id=location.guild_id or 0,
            channel_id=location.channel_id,
            thread_id=location.thread_id,
        )
        if binding is None:
            return False
        content = (message.content or "").strip()
        if not content:
            return False
        audit_fields = dict(
            discord_user_id=str(message.author.id),
            guild_id=str(location.guild_id) if location.guild_id is not None else None,
            channel_id=str(location.channel_id),
            thread_id=str(location.thread_id) if location.thread_id is not None else None,
            action="thread_send",
            herdr_target=binding.herdr_target,
            payload_preview=truncate(content, max_chars=200),
        )
        try:
            self.security.ensure_send_allowed(message.author.id, location, content)
            self.client.send(binding.herdr_target, content)
            self.store.add_audit(AuditEntry(result="ok", **audit_fields))
            await _react(message, "✅")
        except Exception as exc:
            self.store.add_audit(
                AuditEntry(result=truncate(f"error: {exc}", max_chars=500), **audit_fields)
            )
            await _react(message, "⚠️")
        return True


async def send_target_followup(
    bot: HerdrDiscordBot,
    interaction: discord.Interaction,
    content: str,
    *,
    view: discord.ui.View | None = None,
) -> discord.WebhookMessage:
    """Send a followup message for a target-scoped command response."""
    kwargs: dict[str, object] = {"wait": True}
    if view is not None:
        kwargs["view"] = view
    return await interaction.followup.send(content, **kwargs)


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

    @herdr.command(name="current", description="Show the Herdr target bound to this location")
    async def current(self, interaction: discord.Interaction) -> None:
        try:
            location = _location(interaction)
            self.bot.security.ensure_read_allowed(location)
            binding = self.bot.store.get_binding(
                guild_id=location.guild_id or 0,
                channel_id=location.channel_id,
                thread_id=location.thread_id,
            )
            if not binding:
                await interaction.response.send_message(
                    "This location is not bound to a Herdr target."
                )
                return
            await interaction.response.defer(thinking=True)
            await show_target_card(
                interaction,
                self.bot,
                user_id=interaction.user.id,
                location=location,
                target_str=binding.herdr_target,
            )
        except Exception as exc:
            await _send_error(interaction, exc)

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
            await self._send_target_followup(
                interaction,
                f"Sent to `{resolved}`.\n"
                + format_tail(output, max_chars=self.bot.config.max_output_chars),
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
        view: discord.ui.View | None = None,
    ) -> discord.WebhookMessage:
        return await send_target_followup(self.bot, interaction, content, view=view)

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
            apply_action(self.bot.client, self.target, self.strategy)
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


CARD_TIMEOUT_SECONDS = 600
PREVIEW_TAIL_LINES = 15


class TargetCardView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: HerdrDiscordBot,
        user_id: int | None,
        location: DiscordLocation | None,
        target_str: str,
    ):
        super().__init__(timeout=CARD_TIMEOUT_SECONDS)
        self.bot = bot
        self.user_id = user_id
        self.location = location
        self.target_str = target_str

    def _check_actor(self, interaction: discord.Interaction) -> bool:
        return self.user_id is None or interaction.user.id == self.user_id

    @discord.ui.button(label="Tail", style=discord.ButtonStyle.secondary)
    async def tail_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        try:
            if self.location is not None:
                self.bot.security.ensure_read_allowed(self.location)
            await interaction.response.defer(thinking=True)
            output = self.bot.client.read(self.target_str, lines=self.bot.config.max_tail_lines)
            await send_target_followup(
                self.bot,
                interaction,
                format_tail(output, max_chars=self.bot.config.max_output_chars),
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @discord.ui.button(label="Bind Thread", style=discord.ButtonStyle.primary)
    async def bind_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        if self.location is None:
            await interaction.response.send_message(
                "Cannot bind from this context.", ephemeral=True
            )
            return
        try:
            self.bot.security.ensure_read_allowed(self.location)
            self.bot.store.upsert_binding(
                guild_id=self.location.guild_id or 0,
                channel_id=self.location.channel_id,
                thread_id=self.location.thread_id,
                herdr_target=self.target_str,
                label=None,
                created_by=interaction.user.id,
            )
            _audit_action(self.bot, interaction, self.location, "bind", self.target_str, "ok")
            await interaction.response.send_message(
                f"Bound this location to `{self.target_str}`.", ephemeral=True
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)
        await show_target_card(
            interaction,
            self.bot,
            user_id=self.user_id,
            location=self.location,
            target_str=self.target_str,
        )

    async def approve_callback(self, interaction: discord.Interaction) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        try:
            if self.location is not None:
                self.bot.security.ensure_approve_allowed(interaction.user.id, self.location)
            else:
                self.bot.security.ensure_approve_user_allowed(interaction.user.id)
            target_info = self.bot.client.resolve_target(self.target_str)
            ensure_blocked_for_approval(target_info)
            strategy = strategy_for(self.bot.config, target_info.agent_name)
            apply_action(self.bot.client, self.target_str, strategy)
            _audit_action(self.bot, interaction, self.location, "approve", self.target_str, "ok")
            await interaction.response.send_message(
                f"Approved `{self.target_str}`.", ephemeral=True
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    async def deny_callback(self, interaction: discord.Interaction) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        try:
            if self.location is not None:
                self.bot.security.ensure_deny_allowed(interaction.user.id, self.location)
            else:
                self.bot.security.ensure_deny_user_allowed(interaction.user.id)
            target_info = self.bot.client.resolve_target(self.target_str)
            ensure_blocked_for_approval(target_info)
            strategy = deny_strategy_for(self.bot.config, target_info.agent_name)
            apply_action(self.bot.client, self.target_str, strategy)
            _audit_action(self.bot, interaction, self.location, "deny", self.target_str, "ok")
            await interaction.response.send_message(
                f"Denied `{self.target_str}`.", ephemeral=True
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    async def stop_callback(self, interaction: discord.Interaction) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        try:
            location = self.location if self.location is not None else _location(interaction)
            self.bot.security.ensure_stop_allowed(interaction.user.id, location)
            stop_strategy_for(self.bot.config)  # validate a strategy is configured
            view = StopConfirmView(
                bot=self.bot,
                user_id=interaction.user.id,
                location=location,
                target_str=self.target_str,
            )
            await interaction.response.send_message(
                f"Stop `{self.target_str}`? This sends the interrupt sequence.",
                view=view,
                ephemeral=True,
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    async def ask_callback(self, interaction: discord.Interaction) -> None:
        if not self._check_actor(interaction):
            await interaction.response.send_message(
                "Only the requesting user can act here.", ephemeral=True
            )
            return
        try:
            location = self.location if self.location is not None else _location(interaction)
            target_info = self.bot.client.resolve_target(self.target_str)
            modal = AskModal(
                bot=self.bot,
                user_id=interaction.user.id,
                location=location,
                target_str=self.target_str,
                alias=target_alias(target_info),
            )
            await interaction.response.send_modal(modal)
        except Exception as exc:
            await _send_error(interaction, exc)


async def show_target_card(
    interaction: discord.Interaction,
    bot: HerdrDiscordBot,
    *,
    user_id: int | None,
    location: DiscordLocation | None,
    target_str: str,
) -> None:
    try:
        if location is not None:
            bot.security.ensure_read_allowed(location)
    except SecurityError as exc:
        await _send_error(interaction, exc)
        return
    try:
        target_info = bot.client.resolve_target(target_str)
    except Exception as exc:
        await _send_error(interaction, exc)
        return
    tail_preview = _safe_tail_preview(bot, target_str)
    content = format_target_card(
        target_info, tail_preview=tail_preview, max_chars=bot.config.max_output_chars
    )
    view = build_target_card_view(
        bot=bot,
        user_id=user_id,
        location=location,
        target_str=target_str,
        target_info=target_info,
    )
    if interaction.response.is_done():
        await interaction.followup.send(content, view=view, wait=True)
    else:
        await interaction.response.send_message(content, view=view)


def build_target_card_view(
    *,
    bot: HerdrDiscordBot,
    user_id: int | None,
    location: DiscordLocation | None,
    target_str: str,
    target_info: HerdrTarget,
) -> TargetCardView:
    view = TargetCardView(bot=bot, user_id=user_id, location=location, target_str=target_str)
    status = (target_info.status or "").casefold()
    if status == "blocked" and bot.config.enable_approve:
        approve_button = discord.ui.Button(label="Approve", style=discord.ButtonStyle.success)
        approve_button.callback = view.approve_callback
        view.add_item(approve_button)
        if bot.config.deny:
            deny_button = discord.ui.Button(label="Deny", style=discord.ButtonStyle.danger)
            deny_button.callback = view.deny_callback
            view.add_item(deny_button)
    if status == "working" and bot.config.enable_stop:
        stop_button = discord.ui.Button(label="Stop", style=discord.ButtonStyle.danger)
        stop_button.callback = view.stop_callback
        view.add_item(stop_button)
    if bot.config.enable_send:
        ask_button = discord.ui.Button(label="Ask", style=discord.ButtonStyle.primary)
        ask_button.callback = view.ask_callback
        view.add_item(ask_button)
    return view


class StopConfirmView(discord.ui.View):
    def __init__(
        self,
        *,
        bot: HerdrDiscordBot,
        user_id: int,
        location: DiscordLocation,
        target_str: str,
    ):
        super().__init__(timeout=60)
        self.bot = bot
        self.user_id = user_id
        self.location = location
        self.target_str = target_str

    @discord.ui.button(label="Yes, stop", style=discord.ButtonStyle.danger)
    async def confirm_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the requesting user can confirm.", ephemeral=True
            )
            return
        try:
            self.bot.security.ensure_stop_allowed(interaction.user.id, self.location)
            strategy = stop_strategy_for(self.bot.config)
            apply_action(self.bot.client, self.target_str, strategy)
            _audit_action(self.bot, interaction, self.location, "stop", self.target_str, "ok")
            self._disable()
            await interaction.response.edit_message(
                content=f"Stopped `{self.target_str}`.", view=self
            )
        except Exception as exc:
            await _send_error(interaction, exc)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "Only the requesting user can cancel.", ephemeral=True
            )
            return
        self._disable()
        await interaction.response.edit_message(content="Cancelled stop.", view=self)

    def _disable(self) -> None:
        for child in self.children:
            if hasattr(child, "disabled"):
                child.disabled = True


class AskModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        bot: HerdrDiscordBot,
        user_id: int,
        location: DiscordLocation,
        target_str: str,
        alias: str,
    ):
        self.bot = bot
        self.user_id = user_id
        self.location = location
        self.target_str = target_str
        super().__init__(title=f"Send to {alias}"[:45])
        self.instruction = discord.ui.TextInput(
            label="Instruction",
            style=discord.TextStyle.paragraph,
            required=True,
            max_length=1900,
        )
        self.add_item(self.instruction)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        message = (self.instruction.value or "").strip()
        try:
            self.bot.security.ensure_send_allowed(self.user_id, self.location, message)
            self.bot.client.send(self.target_str, message)
            _audit_action(
                self.bot,
                interaction,
                self.location,
                "send",
                self.target_str,
                "ok",
                preview=message,
            )
            await interaction.response.send_message(
                f"Sent to `{self.target_str}`.", ephemeral=True
            )
        except Exception as exc:
            await _send_error(interaction, exc)


def _audit_action(
    bot: HerdrDiscordBot,
    interaction: discord.Interaction,
    location: DiscordLocation | None,
    action: str,
    target: str | None,
    result: str,
    *,
    preview: str | None = None,
) -> None:
    try:
        bot.store.add_audit(
            AuditEntry(
                discord_user_id=str(interaction.user.id),
                guild_id=str(location.guild_id)
                if location and location.guild_id is not None
                else None,
                channel_id=str(location.channel_id) if location else None,
                thread_id=str(location.thread_id) if location and location.thread_id else None,
                action=action,
                herdr_target=target,
                payload_preview=truncate(preview, max_chars=200) if preview else None,
                result=truncate(result, max_chars=500),
            )
        )
    except Exception:
        LOG.exception("Failed to write audit log")


def _safe_tail_preview(bot: HerdrDiscordBot, target_str: str) -> str:
    try:
        return bot.client.read(target_str, lines=min(PREVIEW_TAIL_LINES, bot.config.max_tail_lines))
    except Exception:
        LOG.exception("Failed to read tail preview for %s", target_str)
        return ""


async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


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
