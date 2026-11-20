from __future__ import annotations

from dataclasses import dataclass, field
import sys

import pytest

from herdrelay.config import (
    AppConfig,
    RouterModelConfig,
    RuntimeConfig,
    TelegramConfig,
    TelegramGeneralRouterConfig,
)
from herdrelay.domain import TargetKey
from herdrelay.models import HerdrTarget
from herdrelay.store import Store
from herdrelay.telegram_router import (
    CommandIntentModel,
    PiCliIntentModel,
    RouterError,
    RoutingDecision,
    RoutingRequest,
)
from herdrelay.telegram_service import TelegramService


@dataclass
class FakeApi:
    batches: list[list[dict]]
    sent: list[dict] = field(default_factory=list)
    reactions: list[dict] = field(default_factory=list)

    async def get_updates(self, **_kwargs):
        return self.batches.pop(0) if self.batches else []

    async def send_text(self, **kwargs):
        self.sent.append(kwargs)
        return ()

    async def set_message_reaction(self, **kwargs):
        self.reactions.append(kwargs)
        return True

    async def close(self):
        return None


@dataclass
class FakeClient:
    target: HerdrTarget
    sent: list[tuple[str, str]] = field(default_factory=list)

    def list_targets(self):
        return [self.target]

    def list_panes(self):
        return [self.target]

    def target_key(self, address):
        assert address == self.target.target
        return TargetKey.orca(instance_id="runtime-1", address=address)

    def resolve_target_key(self, key):
        if key != self.target_key(self.target.target):
            raise ValueError("target identity mismatch")
        return self.target

    def send(self, address, text):
        self.sent.append((address, text))


@dataclass
class FakeModel:
    decision: RoutingDecision
    provider_name: str = "fake"
    model_name: str | None = "test-router"
    requests: list = field(default_factory=list)

    async def classify(self, request):
        self.requests.append(request)
        return self.decision


def update(text: str, *, update_id: int = 10):
    return {
        "update_id": update_id,
        "message": {
            "message_id": 5,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": -1001, "type": "supergroup", "is_forum": True},
            "text": text,
        },
    }


def config() -> AppConfig:
    return AppConfig(
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
        ),
    )


def service(tmp_path, model, text):
    app_config = config()
    target = HerdrTarget(
        target="local/tab:pane",
        agent_name="pi",
        status="done",
        workspace_label="herdr-chat-bridge",
        tab_label="Pi main",
    )
    client = FakeClient(target)
    store = Store(tmp_path / "router.sqlite3")
    key = client.target_key(target.target)
    store.upsert_telegram_topic_for_target(
        target=key,
        chat_id=-1001,
        thread_id=168,
        name="herdr-chat-bridge/pi · local/tab:pane",
    )
    api = FakeApi([[update(text)]])
    relay = TelegramService(
        config=app_config,
        store=store,
        client=client,
        api=api,
        router_model=model,
    )
    return relay, client, api, store


@pytest.mark.asyncio
async def test_general_text_is_routed_to_exact_live_candidate(tmp_path):
    utterance = "チャットブリッジのパイにテストを実行して"
    model = FakeModel(
        RoutingDecision(
            intent="send_prompt",
            candidate_id="candidate-1",
            message_verbatim="テストを実行して",
            ambiguous=False,
            reason=None,
            routing_basis="explicit_destination",
        )
    )
    relay, client, api, store = service(tmp_path, model, utterance)

    await relay.poll_once()

    assert client.sent == [("local/tab:pane", "テストを実行して")]
    assert api.reactions == [{"chat_id": -1001, "message_id": 5, "emoji": "👀"}]
    assert api.sent[-1]["message_thread_id"] is None
    assert api.sent[-1]["text"].startswith("→ herdr-chat-bridge/pi")
    request = model.requests[0].public_data()
    assert request["candidates"][0]["id"] == "candidate-1"
    assert "local/tab:pane" not in str(request)
    actions = [entry["action"] for entry in store.list_transport_audits()]
    assert actions == ["route_text", "send_text"]


@pytest.mark.asyncio
async def test_general_followup_uses_recent_context_selected_by_model(tmp_path):
    model = FakeModel(
        RoutingDecision(
            intent="send_prompt",
            candidate_id="candidate-1",
            message_verbatim="状況を教えて",
            ambiguous=False,
            reason=None,
            routing_basis="explicit_destination",
        )
    )
    relay, client, api, store = service(
        tmp_path,
        model,
        "チャットブリッジのパイに状況を教えて",
    )

    await relay.poll_once()

    model.decision = RoutingDecision(
        intent="send_prompt",
        candidate_id="candidate-1",
        message_verbatim="コミットは最新ですか?",
        ambiguous=False,
        reason="continuation of the recent destination",
        routing_basis="recent_context",
    )
    api.batches.append([update("コミットは最新ですか?", update_id=11)])
    await relay.poll_once()

    assert client.sent == [
        ("local/tab:pane", "状況を教えて"),
        ("local/tab:pane", "コミットは最新ですか?"),
    ]
    request = model.requests[-1].public_data()
    assert request["recent_context"]["candidate_id"] == "candidate-1"
    assert request["recent_context"]["workspace"] == "herdr-chat-bridge"
    audits = store.list_transport_audits()
    assert "basis=recent_context" in audits[-2]["detail"]


@pytest.mark.asyncio
async def test_forget_clears_recent_general_context(tmp_path):
    model = FakeModel(
        RoutingDecision(
            intent="send_prompt",
            candidate_id="candidate-1",
            message_verbatim="状況を教えて",
            ambiguous=False,
            reason=None,
            routing_basis="explicit_destination",
        )
    )
    relay, _client, api, store = service(
        tmp_path,
        model,
        "チャットブリッジのパイに状況を教えて",
    )
    await relay.poll_once()
    context_key = "telegram:general-context:-1001:42"
    assert store.get_state(context_key) is not None

    api.batches.append([update("/forget", update_id=11)])
    await relay.poll_once()

    assert store.get_state(context_key) is None
    assert api.sent[-1]["text"] == "直近のGeneral送信先を解除しました。"
    assert store.list_transport_audits()[-1]["action"] == "route_context"


@pytest.mark.asyncio
async def test_general_router_rejects_ambiguous_decision_without_mutation(tmp_path):
    model = FakeModel(
        RoutingDecision(
            intent="unknown",
            candidate_id=None,
            message_verbatim=None,
            ambiguous=True,
            reason="two matching Pi agents",
        )
    )
    relay, client, api, store = service(
        tmp_path, model, "チャットブリッジのパイに続けて"
    )

    await relay.poll_once()

    assert client.sent == []
    assert api.sent[-1]["text"].startswith("送信先を一つに決められませんでした")
    [audit] = store.list_transport_audits()
    assert audit["action"] == "route_text"
    assert audit["outcome"] == "rejected"


@pytest.mark.asyncio
async def test_bound_topic_bypasses_general_router(tmp_path):
    model = FakeModel(
        RoutingDecision(
            intent="unknown",
            candidate_id=None,
            message_verbatim=None,
            ambiguous=True,
            reason=None,
        )
    )
    relay, client, api, _store = service(tmp_path, model, "direct prompt")
    api.batches = [[
        {
            **update("direct prompt"),
            "message": {
                **update("direct prompt")["message"],
                "message_thread_id": 168,
            },
        }
    ]]

    await relay.poll_once()

    assert model.requests == []
    assert client.sent == [("local/tab:pane", "direct prompt")]


@pytest.mark.asyncio
async def test_command_model_uses_json_stdin_and_structured_stdout(tmp_path):
    seed_model = FakeModel(
        RoutingDecision(
            intent="unknown",
            candidate_id=None,
            message_verbatim=None,
            ambiguous=True,
            reason=None,
        )
    )
    relay, _client, _api, _store = service(
        tmp_path, seed_model, "チャットブリッジのパイにテスト"
    )
    candidates = await relay.general_router._candidates()
    request = RoutingRequest(
        utterance="チャットブリッジのパイにテスト",
        candidates=candidates,
    )
    script = (
        "import json,sys; data=json.load(sys.stdin); "
        "assert data['candidates'][0]['workspace']=='herdr-chat-bridge'; "
        "print(json.dumps({'intent':'send_prompt','candidate_id':'candidate-1',"
        "'message_verbatim':'テスト','ambiguous':False,"
        "'routing_basis':'explicit_destination','reason':None}, "
        "ensure_ascii=False))"
    )
    model = CommandIntentModel(
        RouterModelConfig(
            provider="command",
            argv=(sys.executable, "-c", script),
            timeout_seconds=2,
        )
    )

    decision = await model.classify(request)

    assert decision.candidate_id == "candidate-1"
    assert decision.message_verbatim == "テスト"


@pytest.mark.asyncio
async def test_command_model_rejects_invented_candidate(tmp_path):
    seed_model = FakeModel(
        RoutingDecision("unknown", None, None, True, None)
    )
    relay, _client, _api, _store = service(tmp_path, seed_model, "テスト")
    request = RoutingRequest(
        utterance="テスト",
        candidates=await relay.general_router._candidates(),
    )
    script = (
        "import json; print(json.dumps({"
        "'intent':'send_prompt','candidate_id':'made-up',"
        "'message_verbatim':'テスト','ambiguous':False,"
        "'routing_basis':'explicit_destination','reason':None}, "
        "ensure_ascii=False))"
    )
    model = CommandIntentModel(
        RouterModelConfig(
            provider="command",
            argv=(sys.executable, "-c", script),
            timeout_seconds=2,
        )
    )

    with pytest.raises(RouterError, match="invented"):
        await model.classify(request)


@pytest.mark.asyncio
async def test_pi_cli_model_runs_ephemeral_without_tools_or_session(
    tmp_path, monkeypatch
):
    seed_model = FakeModel(
        RoutingDecision("unknown", None, None, True, None)
    )
    relay, _client, _api, _store = service(
        tmp_path, seed_model, "チャットブリッジのパイにテスト"
    )
    request = RoutingRequest(
        utterance="チャットブリッジのパイにテスト",
        candidates=await relay.general_router._candidates(),
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-leak")
    executable = tmp_path / "fake-pi"
    executable.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
args = sys.argv[1:]
required = {'--print', '--no-session', '--no-tools', '--no-extensions', '--no-mcp', '--no-context-files'}
assert required.issubset(args)
assert args[args.index('--extension') + 1] == 'provider.ts'
assert 'TELEGRAM_BOT_TOKEN' not in os.environ
data = json.loads(args[-1])
assert data['candidates'][0]['id'] == 'candidate-1'
print(json.dumps({
  'intent': 'send_prompt',
  'candidate_id': 'candidate-1',
  'message_verbatim': 'テスト',
  'ambiguous': False,
  'routing_basis': 'explicit_destination',
  'reason': None,
}, ensure_ascii=False))
"""
    )
    executable.chmod(0o700)
    model = PiCliIntentModel(
        RouterModelConfig(
            provider="pi-cli",
            model="provider/router-small",
            cli_path=str(executable),
            node_path=sys.executable,
            extensions=("provider.ts",),
            timeout_seconds=2,
        )
    )

    decision = await model.classify(request)

    assert decision.candidate_id == "candidate-1"
    assert decision.message_verbatim == "テスト"
