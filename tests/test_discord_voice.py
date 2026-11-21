from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from herdrelay.config import (
    AppConfig,
    TelegramConfig,
    TelegramGeneralRouterConfig,
    VoiceInputConfig,
)
from herdrelay.discord_bot import HerdrDiscordBot
from herdrelay.discord_voice import is_discord_voice_message
from herdrelay.domain import TargetKey
from herdrelay.models import HerdrTarget
from herdrelay.store import Store
from herdrelay.voice_transcription import Transcript


class FakeAttachment:
    content_type = "audio/ogg"
    filename = "voice-message.ogg"
    duration = 2.5

    def __init__(self, data: bytes = b"voice"):
        self.data = data
        self.size = len(data)

    async def save(self, path: Path) -> int:
        path.write_bytes(self.data)
        return len(self.data)


class FakeTranscriber:
    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self, text: str = "音声テスト"):
        self.text = text
        self.paths: list[Path] = []

    async def transcribe(self, path: Path) -> Transcript:
        assert path.read_bytes() == b"voice"
        self.paths.append(path)
        return Transcript(text=self.text, language="ja", duration_seconds=2.5)


class FakeClient:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def resolve_target_key(self, key: TargetKey) -> HerdrTarget:
        assert key == TargetKey.legacy("w1:p1")
        return HerdrTarget(target="w1:p1", agent_name="pi", status="idle")

    def send(self, target: str, text: str) -> None:
        self.sent.append((target, text))


def config(tmp_path, *, scope: str = "general-and-bound") -> AppConfig:
    voice = VoiceInputConfig(enabled=True, scope=scope)
    return AppConfig(
        discord_token="token",
        database_path=str(tmp_path / "store.sqlite3"),
        allowed_guild_ids=frozenset({1}),
        allowed_channel_ids=frozenset({100}),
        allowed_user_ids=frozenset({42}),
        thread_parent_channel_id=100,
        enable_send=True,
        voice_input=voice,
        telegram=TelegramConfig(
            general_router=TelegramGeneralRouterConfig(
                enabled=True,
                provider="router",
            ),
            voice_input=voice,
        ),
    )


def message(*, channel_id: int = 100, parent_id: int | None = None):
    return SimpleNamespace(
        id=123,
        content="",
        mentions=[],
        flags=SimpleNamespace(voice=True),
        attachments=[FakeAttachment()],
        author=SimpleNamespace(id=42, bot=False),
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=channel_id, parent_id=parent_id),
        reply=AsyncMock(),
        add_reaction=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_discord_general_voice_is_transcribed_and_forwarded(tmp_path):
    transcriber = FakeTranscriber()
    bot = HerdrDiscordBot(
        config=config(tmp_path),
        store=Store(tmp_path / "store.sqlite3"),
        client=FakeClient(),
        router_model=SimpleNamespace(
            provider_name="fake", model_name=None, classify=AsyncMock()
        ),
        voice_transcriber=transcriber,
    )
    general = SimpleNamespace(handle=AsyncMock(return_value=True))
    bot.general_handler = general
    inbound = message()

    await bot.on_message(inbound)

    general.handle.assert_awaited_once_with(
        inbound,
        text="音声テスト",
        source_kind="voice",
    )
    assert inbound.reply.await_args.args[0] == "Transcript:\n音声テスト"
    with bot.store._connect() as connection:
        audit = connection.execute(
            "SELECT action, outcome, payload_preview FROM transport_audit"
        ).fetchone()
    assert tuple(audit) == ("transcribe_voice", "succeeded", "音声テスト")


@pytest.mark.asyncio
async def test_discord_bound_voice_submits_directly_to_exact_agent(tmp_path):
    app_config = config(tmp_path)
    store = Store(app_config.database_path)
    store.upsert_binding(
        guild_id=1,
        channel_id=100,
        thread_id=200,
        herdr_target=TargetKey.legacy("w1:p1").to_route_value(),
        label="test",
        created_by=42,
    )
    client = FakeClient()
    bot = HerdrDiscordBot(
        config=app_config,
        store=store,
        client=client,
        router_model=SimpleNamespace(
            provider_name="fake", model_name=None, classify=AsyncMock()
        ),
        voice_transcriber=FakeTranscriber("bound voice"),
    )
    inbound = message(channel_id=200, parent_id=100)

    await bot.on_message(inbound)

    assert client.sent == [("w1:p1", "bound voice")]
    inbound.add_reaction.assert_awaited_once_with("✅")
    with store._connect() as connection:
        audit = connection.execute(
            "SELECT action, result, payload_preview FROM audit_log"
        ).fetchone()
    assert tuple(audit) == ("thread_send_voice", "ok", "bound voice")


def test_discord_audio_attachment_requires_native_voice_flag():
    inbound = message()
    inbound.flags.voice = False

    assert is_discord_voice_message(inbound) is False
