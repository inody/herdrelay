from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .config import AppConfig
from .domain import MutationOutcomeUnknownError, TargetKey
from .formatter import (
    target_alias,
    truncate,
)
from .hc_client import HcClient
from .core_discord_watcher import CoreEventWatcher
from .herdr_client import TargetResolutionError
from .models import AuditEntry, PendingQuestion
from .questions import QuestionAnswerError, parse_question_answer, selected_labels
from .runtime import InterruptRuntime, QuestionAnswerRuntime, RuntimeClient
from .discord_security import DiscordLocation, SecurityError, SecurityPolicy
from .store import Store
from .discord_output import StreamManager
from .discord_threads import ThreadManager
from .discord_watcher import EventWatcher

LOG = logging.getLogger(__name__)


class HerdrDiscordBot(commands.Bot):
    def __init__(self, *, config: AppConfig, store: Store, client: RuntimeClient):
        intents = discord.Intents.default()
        if config.enable_send or config.enable_questions:
            intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.store = store
        self.client = client
        self.security = SecurityPolicy(config)
        self._watcher_task: asyncio.Task | None = None
        self._watcher: EventWatcher | CoreEventWatcher | None = None
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
        if self.config.enable_watcher and self.config.watcher.statuses:
            watcher_type = EventWatcher
            if self.config.runtime.provider == "herdr-core":
                if not isinstance(self.client, HcClient):
                    raise TypeError("Herdr Core runtime requires HcClient")
                watcher_type = CoreEventWatcher
            self._watcher = watcher_type(
                bot=self,
                config=self.config,
                store=self.store,
                client=self.client,
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
        question_choices_enabled = self.config.enable_questions and isinstance(
            self.client, QuestionAnswerRuntime
        )
        # Legacy TUI question choices wait indefinitely, so restore them after
        # a restart only when structured question answering is enabled.
        if question_choices_enabled:
            for question in self.store.list_pending_questions():
                self.add_view(QuestionChoiceView(bot=self, question=question))
        if self.config.enable_streaming:
            self.stream_manager = StreamManager(
                bot=self,
                config=self.config,
                store=self.store,
                client=self.client,
                question_view_factory=(
                    self._question_view_for_event if question_choices_enabled else None
                ),
            )
            self._stream_task = asyncio.create_task(self.stream_manager.run_forever())
            LOG.info("Started output relay")

    async def on_ready(self) -> None:
        # Nickname the bot "herdr" so @he autocomplete points here.
        for guild in self.guilds:
            try:
                if guild.me.nick != "herdr":
                    await guild.me.edit(nick="herdr")
            except discord.HTTPException:
                LOG.warning("Failed to set nickname in guild %s", guild.id)
        LOG.info("Logged in as %s", self.user)

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

    def _question_view_for_event(self, question: PendingQuestion) -> discord.ui.View:
        return QuestionChoiceView(bot=self, question=question)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        if self.user and self.user in message.mentions:
            await self._handle_control_command(message)
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
            pending_question = self.store.get_pending_question(binding.herdr_target)
            if pending_question is not None and content.isdecimal():
                selected = parse_question_answer(content, pending_question)
                if selected is None:
                    raise QuestionAnswerError(
                        f"Choose a number from 1 to {len(pending_question.options)}."
                    )
                self.security.ensure_question_answer_allowed(message.author.id, location)
                labels = await self._answer_pending_question(pending_question, selected)
                audit_fields["action"] = "question_select"
                audit_fields["payload_preview"] = labels
            else:
                self.security.ensure_send_allowed(message.author.id, location, content)
                target_info = await asyncio.to_thread(
                    self.client.resolve_target_key,
                    TargetKey.from_route_value(binding.herdr_target),
                )
                await asyncio.to_thread(self.client.send, target_info.target, content)
            self.store.add_audit(AuditEntry(result="ok", **audit_fields))
            await _react(message, "✅")
        except MutationOutcomeUnknownError as exc:
            self.store.add_audit(
                AuditEntry(
                    result=truncate(f"outcome_unknown: {exc}", max_chars=500),
                    **audit_fields,
                )
            )
            await _react(message, "⚠️")
        except Exception as exc:
            self.store.add_audit(
                AuditEntry(result=truncate(f"error: {exc}", max_chars=500), **audit_fields)
            )
            await _react(message, "⚠️")
        return True

    async def _answer_pending_question(
        self, question: PendingQuestion, selected: tuple[int, ...]
    ) -> str:
        if not isinstance(self.client, QuestionAnswerRuntime):
            raise QuestionAnswerError("Question answering is unavailable for this backend.")
        current = self.store.get_pending_question(question.pane_id)
        if current is None or current.event_id != question.event_id:
            raise QuestionAnswerError("This question is no longer pending.")
        target_info = await asyncio.to_thread(self.client.resolve_target, question.pane_id)
        status = (target_info.status or "unknown").casefold()
        if status != "blocked":
            raise QuestionAnswerError(
                f"Question answering requires blocked status; current status is {status}."
            )
        # Claim the question before the backend operation so a second click
        # cannot submit a competing answer.
        self.store.clear_pending_question(question.pane_id, event_id=question.event_id)
        try:
            await asyncio.to_thread(
                self.client.answer_question,
                question,
                selected,
            )
        except Exception:
            self.store.upsert_pending_question(question)
            raise
        return selected_labels(question, selected)

    async def _handle_control_command(self, message: discord.Message) -> None:
        content = message.content
        if self.user:
            content = (
                content.replace(f"<@!{self.user.id}>", "")
                .replace(f"<@{self.user.id}>", "")
                .strip()
            )
        parts = content.split()
        cmd = parts[0].lower() if parts else "help"
        if cmd == "?":
            cmd = "help"
        args = parts[1:]
        location = _location_from_message(message)
        action = "interrupt" if cmd == "stop" else cmd
        try:
            # Check before dispatch, including help/unknown commands. In
            # particular, do not resolve a bound target or read the backend first.
            self.security.ensure_read_allowed(location)
            if action == "interrupt":
                self.security.ensure_interrupt_allowed(message.author.id, location)
                await self._control_interrupt(message, args)
            elif action == "list":
                await self._control_list(message)
                self._audit_control_command(message, action, "ok")
            elif action == "help":
                await message.reply(self._control_help())
                self._audit_control_command(message, action, "ok")
            else:
                raise ValueError(
                    f"Unknown command `{cmd}`. Try: interrupt, list, help"
                )
        except Exception as exc:
            result = "denied" if isinstance(exc, SecurityError) else "error"
            try:
                self._audit_control_command(
                    message,
                    action,
                    f"{result}: {exc}",
                    target=args[0] if action == "interrupt" and args else None,
                )
            except Exception:
                LOG.exception("Failed to audit rejected/failed mention command")
            await message.reply(
                f"Error: {exc}", allowed_mentions=discord.AllowedMentions.none(),
                mention_author=False,
            )

    def _audit_control_command(
        self, message: discord.Message, command: str, result: str, *, target: str | None = None,
    ) -> None:
        location = _location_from_message(message)
        action = command if command in {"interrupt", "list", "help"} else "unknown"
        self.store.add_audit(AuditEntry(
            discord_user_id=str(message.author.id),
            guild_id=str(location.guild_id) if location.guild_id is not None else None,
            channel_id=str(location.channel_id),
            thread_id=str(location.thread_id) if location.thread_id is not None else None,
            action=f"control_{action}",
            herdr_target=target,
            payload_preview=truncate(message.content, max_chars=200),
            result=truncate(result, max_chars=500),
        ))

    async def _control_interrupt(
        self, message: discord.Message, args: list[str]
    ) -> None:
        target_address = args[0] if args else None
        if target_address is None:
            parent_id = getattr(message.channel, "parent_id", None)
            if parent_id is not None:
                location = _location_from_message(message)
                binding = self.store.get_binding(
                    guild_id=location.guild_id or 0,
                    channel_id=location.channel_id,
                    thread_id=location.thread_id,
                )
                if binding:
                    target = await asyncio.to_thread(
                        self.client.resolve_target_key,
                        TargetKey.from_route_value(binding.herdr_target),
                    )
                    target_address = target.target
        if target_address is None:
            raise ValueError(
                "Usage: `@herdr interrupt <target>` (or run inside an agent thread)"
            )
        if not (
            self.client.capabilities.native_interrupt
            and isinstance(self.client, InterruptRuntime)
        ):
            raise ValueError("Interrupt is unavailable for this backend.")
        self._audit_control_command(
            message, "interrupt", "attempt", target=target_address
        )
        await asyncio.to_thread(self.client.interrupt, target_address)
        self._audit_control_command(
            message, "interrupt", "ok", target=target_address
        )
        await message.reply(f"Interrupted `{target_address}`.")

    async def _control_list(self, message: discord.Message) -> None:
        targets = await asyncio.to_thread(self.client.list_targets)
        if not targets:
            await message.reply("No Herdr agents or panes found.")
            return
        lines = []
        for t in targets:
            status = t.status or "unknown"
            alias = target_alias(t)
            agent = t.agent_name or t.kind or "?"
            lines.append(f"{status:<8} {t.target:<8} {alias}/{agent}")
        await message.reply("```text\n" + "\n".join(lines) + "\n```")

    def _control_help(self) -> str:
        lines = [
            "**Runtime control** (mention me)",
            "```",
        ]
        if (
            self.config.enable_interrupt
            and self.client.capabilities.native_interrupt
            and isinstance(self.client, InterruptRuntime)
        ):
            lines.extend(
                (
                    "@herdr interrupt [<target>]                 Interrupt a working agent",
                    "  inside an agent thread: interrupts that target (no arg needed)",
                )
            )
        lines.extend(
            (
                "@herdr list                                    List available agents",
                "@herdr help                                    Show this help",
                "```",
                "Inside an agent thread, normal text submits a prompt.",
            )
        )
        return "\n".join(lines)


QUESTION_COMPONENT_MAX_OPTIONS = 25


class QuestionChoiceView(discord.ui.View):
    """Choices for a pending Claude question.

    The view never times out and uses custom IDs derived from the question, so
    it can be re-registered after a restart: Claude waits for the answer as
    long as it takes, and an expired view makes Discord fail every click
    without reaching the bot.
    """

    def __init__(self, *, bot: HerdrDiscordBot, question: PendingQuestion):
        super().__init__(timeout=None)
        self.bot = bot
        self.question = question
        id_prefix = f"herdrelay:question:{question.event_id[:64]}"
        if not question.multi_select and len(question.options) <= 5:
            for index, option in enumerate(question.options):
                button = discord.ui.Button(
                    label=_question_button_label(index, option.label),
                    style=discord.ButtonStyle.primary,
                    custom_id=f"{id_prefix}:{index}",
                )
                button.callback = self._button_callback(index)
                self.add_item(button)
        elif len(question.options) <= QUESTION_COMPONENT_MAX_OPTIONS:
            select = discord.ui.Select(
                custom_id=f"{id_prefix}:select",
                placeholder="Choose option(s)…",
                min_values=1,
                max_values=len(question.options) if question.multi_select else 1,
                options=[
                    discord.SelectOption(
                        label=_question_button_label(index, option.label),
                        value=str(index),
                        description=(option.description or "")[:100] or None,
                    )
                    for index, option in enumerate(question.options)
                ],
            )
            select.callback = self._select_callback
            self.add_item(select)

    def _button_callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            await self._answer(interaction, (index,))

        return callback

    async def _select_callback(self, interaction: discord.Interaction) -> None:
        values = self.children[0].values  # type: ignore[union-attr]
        await self._answer(interaction, tuple(int(value) for value in values))

    async def _answer(
        self, interaction: discord.Interaction, selected: tuple[int, ...]
    ) -> None:
        try:
            self.bot.security.ensure_question_answer_allowed(interaction.user.id)
            # Typing the answer takes longer than Discord's 3-second reply window.
            await interaction.response.defer()
            labels = await self.bot._answer_pending_question(self.question, selected)
            _audit_action(
                self.bot,
                interaction,
                None,
                "question_select",
                self.question.pane_id,
                "ok",
                preview=labels,
            )
            for child in self.children:
                child.disabled = True
            self.stop()
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(f"Selected: {labels}", ephemeral=True)
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


async def _react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


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


def _question_button_label(index: int, label: str) -> str:
    return f"{index + 1}. {label}"[:80]


async def _send_error(interaction: discord.Interaction, exc: Exception) -> None:
    if isinstance(exc, (SecurityError, TargetResolutionError, QuestionAnswerError)):
        LOG.info("Discord command rejected: %s", exc)
        message = str(exc)
    else:
        LOG.exception("Discord command failed")
        message = f"Command failed: {exc}"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def build_bot(config: AppConfig, store: Store, client: RuntimeClient) -> HerdrDiscordBot:
    return HerdrDiscordBot(config=config, store=store, client=client)
