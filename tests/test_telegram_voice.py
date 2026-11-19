from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from herdrelay.config import (
    AppConfig,
    RouterModelConfig,
    RuntimeConfig,
    TelegramConfig,
    TelegramGeneralRouterConfig,
    TelegramVoiceInputConfig,
    VoiceTranscriptionConfig,
)
from herdrelay.domain import TargetKey
from herdrelay.models import HerdrTarget
from herdrelay.store import Store
from herdrelay.telegram_router import RoutingDecision
from herdrelay.telegram_service import TelegramService
from herdrelay.voice_transcription import Transcript


@dataclass
class FakeApi:
    batches: list[list[dict]]
    sent: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)
    downloads: list[dict] = field(default_factory=list)

    async def get_updates(self, **_kwargs):
        return self.batches.pop(0) if self.batches else []

    async def send_text(self, **kwargs):
        self.sent.append(kwargs)
        return ()

    async def set_message_reaction(self, **kwargs):
        self.reactions.append(kwargs)
        return True

    async def download_file(self, **kwargs):
        self.downloads.append(kwargs)
        Path(kwargs["destination"]).write_bytes(b"OggS audio")
        return None

    async def close(self):
        return None


@dataclass
class FakeClient:
    target: HerdrTarget
    sent: list[tuple[str, str]] = field(default_factory=list)

    def list_targets(self):
        return [self.target]

    def target_key(self, address):
        return TargetKey.orca(instance_id="runtime-1", address=address)

    def send(self, address, text):
        self.sent.append((address, text))


@dataclass
class FakeModel:
    requests: list = field(default_factory=list)
    provider_name: str = "fake"
    model_name: str | None = "router"

    async def classify(self, request):
        self.requests.append(request)
        return RoutingDecision(
            intent="send_prompt",
            candidate_id="candidate-1",
            message_verbatim="現在の状態を教えて",
            ambiguous=False,
            reason=None,
            routing_basis="explicit_destination",
        )


@dataclass
class FakeTranscriber:
    text: str = "HJB FDMのパイに現在の状態を教えて"
    provider_name: str = "fake-stt"
    model_name: str | None = "whisper"
    paths: list[Path] = field(default_factory=list)

    async def transcribe(self, path):
        self.paths.append(path)
        assert path.exists()
        return Transcript(self.text, language="ja", duration_seconds=3.0)


def voice_update(*, duration=3, file_size=1024, thread_id=None):
    message = {
        "message_id": 5,
        "from": {"id": 42, "is_bot": False},
        "chat": {"id": -1001, "type": "supergroup", "is_forum": True},
        "voice": {
            "file_id": "voice-file",
            "file_unique_id": "voice-unique",
            "duration": duration,
            "mime_type": "audio/ogg",
            "file_size": file_size,
        },
    }
    if thread_id is not None:
        message["message_thread_id"] = thread_id
    return {"update_id": 10, "message": message}


def build_service(
    tmp_path,
    api,
    model,
    transcriber,
    *,
    voice_scope="general",
):
    config = AppConfig(
        discord_token="unused",
        runtime=RuntimeConfig(provider="orca"),
        telegram=TelegramConfig(
            enabled=True,
            token="token",
            allowed_chat_ids=frozenset({-1001}),
            allowed_user_ids=frozenset({42}),
            forum_chat_id=-1001,
            enable_send=True,
            general_router=TelegramGeneralRouterConfig(
                enabled=True,
                context_ttl_seconds=1800,
                model=RouterModelConfig(provider="none"),
            ),
            voice_input=TelegramVoiceInputConfig(
                enabled=True,
                scope=voice_scope,
                max_duration_seconds=120,
                max_file_bytes=10_000,
                transcription=VoiceTranscriptionConfig(
                    provider="mlx-whisper",
                    model="model",
                ),
            ),
        ),
    )
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="pi",
        status="done",
        workspace_label="HJB_FDM",
    )
    client = FakeClient(target)
    store = Store(tmp_path / "voice.sqlite3")
    store.upsert_telegram_topic_for_target(
        target=client.target_key(target.target),
        chat_id=-1001,
        thread_id=179,
        name="HJB_FDM/pi",
    )
    return TelegramService(
        config=config,
        store=store,
        client=client,
        api=api,
        router_model=model,
        voice_transcriber=transcriber,
    ), client, store


@pytest.mark.asyncio
async def test_general_voice_is_transcribed_then_routed_through_normal_prompt_path(
    tmp_path,
):
    api = FakeApi([[voice_update()]])
    model = FakeModel()
    transcriber = FakeTranscriber()
    service, client, store = build_service(tmp_path, api, model, transcriber)

    await service.poll_once()

    assert len(api.downloads) == 1
    assert api.downloads[0]["max_bytes"] == 10_000
    assert client.sent == [("local/tab:pane", "現在の状態を教えて")]
    assert model.requests[0].utterance == transcriber.text
    public_request = model.requests[0].public_data()
    assert public_request["source_kind"] == "voice"
    assert "asr_candidate_hints" not in public_request
    assert "speech_keys" not in public_request["candidates"][0]
    assert api.sent[0]["text"].startswith("文字起こし:\nHJB FDM")
    assert api.sent[-1]["text"].startswith("→ HJB_FDM/pi")
    assert not transcriber.paths[0].exists()
    assert [entry["action"] for entry in store.list_transport_audits()] == [
        "transcribe_voice",
        "route_text",
        "send_text",
    ]


@pytest.mark.asyncio
async def test_voice_over_limit_is_rejected_without_download_or_mutation(tmp_path):
    api = FakeApi([[voice_update(duration=121)]])
    service, client, store = build_service(
        tmp_path, api, FakeModel(), FakeTranscriber()
    )

    await service.poll_once()

    assert api.downloads == []
    assert client.sent == []
    assert "長すぎる" in api.sent[-1]["text"]
    [audit] = store.list_transport_audits()
    assert audit["action"] == "transcribe_voice"
    assert audit["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_voice_in_bound_topic_is_sent_directly_without_router(tmp_path):
    api = FakeApi([[voice_update(thread_id=179)]])
    model = FakeModel()
    transcriber = FakeTranscriber(text="このまま続けてください")
    service, client, store = build_service(
        tmp_path,
        api,
        model,
        transcriber,
        voice_scope="general-and-bound",
    )

    await service.poll_once()

    assert client.sent == [("local/tab:pane", "このまま続けてください")]
    assert model.requests == []
    assert api.sent[0]["message_thread_id"] == 179
    assert api.sent[0]["text"].startswith("文字起こし:\n")
    assert [entry["action"] for entry in store.list_transport_audits()] == [
        "transcribe_voice",
        "send_text",
    ]


@pytest.mark.asyncio
async def test_voice_in_bound_topic_is_not_transcribed_when_scope_is_general(tmp_path):
    api = FakeApi([[voice_update(thread_id=179)]])
    service, client, _store = build_service(
        tmp_path, api, FakeModel(), FakeTranscriber()
    )

    await service.poll_once()

    assert api.downloads == []
    assert client.sent == []
    assert "General" in api.sent[-1]["text"]
