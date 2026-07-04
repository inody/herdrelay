from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .approval import (
    ApprovalError,
    apply_action,
    deny_strategy_for,
    ensure_blocked_for_approval,
    stop_strategy_for,
    strategy_for,
)
from .config import AppConfig
from .formatter import (
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
        # Sync the (empty) command tree so Discord drops any previously
        # registered slash commands (e.g. legacy /herdr status|ids|current).
        if self.config.allowed_guild_ids:
            for guild_id in self.config.allowed_guild_ids:
                await self.tree.sync(guild=discord.Object(id=guild_id))
                LOG.info("Synced command tree to guild %s", guild_id)
        else:
            await self.tree.sync()
            LOG.info("Synced global command tree")
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
            keys = _parse_special_keys(content)
            if keys is not None:
                self.client.send_keys(binding.herdr_target, keys)
                audit_fields["action"] = "key_send"
            else:
                self.client.send(binding.herdr_target, content)
            self.store.add_audit(AuditEntry(result="ok", **audit_fields))
            await _react(message, "✅")
        except Exception as exc:
            self.store.add_audit(
                AuditEntry(result=truncate(f"error: {exc}", max_chars=500), **audit_fields)
            )
            await _react(message, "⚠️")
        return True


SPECIAL_KEYS: dict[str, tuple[str, ...]] = {
    "esc": ("Escape",),
    "escape": ("Escape",),
    "enter": ("Enter",),
    "return": ("Enter",),
    "tab": ("Tab",),
    "space": ("Space",),
    "up": ("Up",),
    "down": ("Down",),
    "left": ("Left",),
    "right": ("Right",),
    "ctrl-c": ("C-c",),
    "ctrlc": ("C-c",),
    "backspace": ("BS",),
    "bs": ("BS",),
    "pgup": ("PageUp",),
    "pgdn": ("PageDown",),
    "home": ("Home",),
    "end": ("End",),
}


def _parse_special_keys(content: str) -> tuple[str, ...] | None:
    """Return key sequence if *content* is a special-key command (!esc, etc.)."""
    if not content.startswith("!"):
        return None
    return SPECIAL_KEYS.get(content[1:].strip().lower())


CARD_TIMEOUT_SECONDS = 600


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
