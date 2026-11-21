from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from .config import AppConfig, RouterModelConfig
from .domain import TargetKey
from .formatter import target_alias, truncate
from .models import HerdrTarget, TelegramTopicRoute, TransportAuditEntry
from .runtime import RuntimeClient
from .store import Store
from .telegram_api import TelegramBotApi
from .telegram_mutations import TelegramMutationHandler

LOG = logging.getLogger(__name__)


class RouterError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RouterCandidate:
    candidate_id: str
    key: TargetKey
    target: HerdrTarget
    route: TelegramTopicRoute | None = None
    conversation_id: str | None = None
    name: str | None = None

    @property
    def destination_id(self) -> str:
        if self.route is not None:
            return self.route.thread_id
        if self.conversation_id is None:
            raise ValueError("router candidate has no destination")
        return self.conversation_id

    @property
    def display_name(self) -> str:
        if self.route is not None:
            return self.route.name
        return self.name or target_alias(self.target)

    def public_data(self) -> dict[str, Any]:
        return {
            "id": self.candidate_id,
            "workspace": self.target.workspace_label,
            "title": self.target.tab_label,
            "agent": self.target.agent_name,
            "status": self.target.status or "unknown",
            "alias": target_alias(self.target),
        }


@dataclass(frozen=True, slots=True)
class RecentRoutingContext:
    candidate: RouterCandidate
    age_seconds: int

    def public_data(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate.candidate_id,
            "age_seconds": self.age_seconds,
            "workspace": self.candidate.target.workspace_label,
            "title": self.candidate.target.tab_label,
            "agent": self.candidate.target.agent_name,
            "status": self.candidate.target.status or "unknown",
            "alias": target_alias(self.candidate.target),
        }


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    utterance: str
    candidates: tuple[RouterCandidate, ...]
    source_kind: str = "text"
    recent_context: RecentRoutingContext | None = None

    def public_data(self) -> dict[str, Any]:
        return {
            "version": 3,
            "source_kind": self.source_kind,
            "utterance": self.utterance,
            "recent_context": (
                self.recent_context.public_data()
                if self.recent_context is not None
                else None
            ),
            "candidates": [candidate.public_data() for candidate in self.candidates],
            "output_contract": {
                "intent": "send_prompt or unknown",
                "candidate_id": "one candidate id or null",
                "message_verbatim": "an exact non-empty substring of utterance or null",
                "ambiguous": "boolean",
                "routing_basis": (
                    "explicit_destination, recent_context, or unknown"
                ),
                "reason": "short string",
            },
        }


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    intent: str
    candidate_id: str | None
    message_verbatim: str | None
    ambiguous: bool
    reason: str | None
    routing_basis: str = "unknown"


class IntentModel(Protocol):
    provider_name: str
    model_name: str | None

    async def classify(self, request: RoutingRequest) -> RoutingDecision: ...


@dataclass(frozen=True, slots=True)
class RoutingResolution:
    outcome: str
    detail: str
    decision: RoutingDecision | None = None
    selected: RouterCandidate | None = None
    prompt: str = ""


async def resolve_routing_request(
    model: IntentModel,
    request: RoutingRequest,
) -> RoutingResolution:
    """Apply the shared fail-closed contract to one model decision."""
    try:
        decision = await model.classify(request)
    except Exception as exc:
        return RoutingResolution(outcome="failed", detail=str(exc))

    candidates = {item.candidate_id: item for item in request.candidates}
    selected = candidates.get(decision.candidate_id or "")
    prompt = decision.message_verbatim or ""
    valid_context_choice = (
        decision.routing_basis != "recent_context"
        or (
            request.recent_context is not None
            and selected is not None
            and selected.candidate_id
            == request.recent_context.candidate.candidate_id
        )
    )
    if (
        decision.intent != "send_prompt"
        or decision.ambiguous
        or decision.routing_basis not in {
            "explicit_destination",
            "recent_context",
        }
        or not valid_context_choice
        or selected is None
        or not prompt
        or prompt not in request.utterance
    ):
        return RoutingResolution(
            outcome="rejected",
            detail=decision.reason or "ambiguous or invalid routing decision",
            decision=decision,
        )
    return RoutingResolution(
        outcome="succeeded",
        detail=(
            f"provider={model.provider_name} "
            f"model={model.model_name or '-'} candidate={selected.candidate_id} "
            f"basis={decision.routing_basis}"
        ),
        decision=decision,
        selected=selected,
        prompt=prompt,
    )


class OpenAICompatibleIntentModel:
    provider_name = "openai-compatible"

    def __init__(self, config: RouterModelConfig):
        if not config.model or not config.base_url:
            raise ValueError("OpenAI-compatible router requires model and base_url")
        self.config = config
        self.model_name = config.model

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key_env:
            token = os.environ.get(self.config.api_key_env, "").strip()
            if not token:
                raise RouterError(
                    f"Router credential environment variable is unset: {self.config.api_key_env}"
                )
            headers["Authorization"] = f"Bearer {token}"
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": 300,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(request.public_data(), ensure_ascii=False),
                },
            ],
        }
        timeout = aiohttp.ClientTimeout(total=self.config.timeout_seconds)
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as response:
                    body = await response.read()
                    if len(body) > self.config.max_output_bytes:
                        raise RouterError("Router model response exceeded the size limit")
                    if response.status >= 400:
                        raise RouterError(
                            f"Router model returned HTTP {response.status}"
                        )
        except asyncio.TimeoutError as exc:
            raise RouterError("Router model timed out") from exc
        except aiohttp.ClientError as exc:
            raise RouterError(f"Router model request failed: {exc}") from exc
        try:
            data = json.loads(body)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise RouterError("Router model returned an invalid response") from exc
        return _parse_decision(content, request=request)


class PiCliIntentModel:
    provider_name = "pi-cli"

    def __init__(self, config: RouterModelConfig):
        if not config.model or not config.cli_path:
            raise ValueError("Pi CLI router requires model and cli_path")
        self.config = config
        self.model_name = config.model

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        prompt = json.dumps(request.public_data(), ensure_ascii=False)
        model_args: tuple[str, ...]
        if "/" in (self.config.model or ""):
            provider, model = (self.config.model or "").split("/", 1)
            model_args = ("--provider", provider, "--model", model)
        else:
            model_args = ("--model", self.config.model or "")
        extension_args = tuple(
            item
            for extension in self.config.extensions
            for item in ("--extension", extension)
        )
        stdout = await _run_command(
            (
                self.config.cli_path,
                "--print",
                "--no-session",
                "--no-tools",
                "--no-extensions",
                "--no-mcp",
                "--no-skills",
                "--no-prompt-templates",
                "--no-themes",
                "--no-context-files",
                *extension_args,
                *model_args,
                "--system-prompt",
                _SYSTEM_PROMPT,
                prompt,
            ),
            input_bytes=None,
            config=self.config,
            label="Pi router",
            env=_pi_environment(self.config),
        )
        return _parse_decision(stdout.decode(errors="strict"), request=request)


class CommandIntentModel:
    provider_name = "command"

    def __init__(self, config: RouterModelConfig):
        if not config.argv:
            raise ValueError("Command router requires argv")
        self.config = config
        self.model_name = config.model

    async def classify(self, request: RoutingRequest) -> RoutingDecision:
        stdout = await _run_command(
            self.config.argv,
            input_bytes=json.dumps(
                request.public_data(), ensure_ascii=False
            ).encode(),
            config=self.config,
            label="Router command",
        )
        return _parse_decision(stdout.decode(errors="strict"), request=request)


def build_intent_model(config: RouterModelConfig) -> IntentModel:
    if config.provider == "openai-compatible":
        return OpenAICompatibleIntentModel(config)
    if config.provider == "pi-cli":
        return PiCliIntentModel(config)
    if config.provider == "command":
        return CommandIntentModel(config)
    raise ValueError(f"Unsupported router model provider: {config.provider}")


class TelegramGeneralRouter:
    def __init__(
        self,
        *,
        config: AppConfig,
        store: Store,
        client: RuntimeClient,
        api: TelegramBotApi,
        mutations: TelegramMutationHandler,
        model: IntentModel | None = None,
    ):
        self.config = config
        self.telegram = config.telegram
        self.router = config.telegram.general_router
        self.store = store
        self.client = client
        self.api = api
        self.mutations = mutations
        self.model = model or build_intent_model(self.router.model)

    def accepts(self, message) -> bool:
        return message.message_thread_id is None or (
            self.router.general_topic_id is not None
            and message.message_thread_id == self.router.general_topic_id
        )

    async def handle(self, message, *, event_key: str) -> bool:
        utterance = message.prompt_text
        if not utterance or len(utterance) > self.config.max_message_chars:
            await self._feedback(
                message, "The input is empty or too long. Nothing was sent."
            )
            return False
        candidates = await self._candidates()
        if not candidates:
            await self._feedback(
                message, "No Agent topics are available. Nothing was sent."
            )
            return False
        recent_context = self._recent_context(message, candidates)
        request = RoutingRequest(
            utterance=utterance,
            candidates=candidates,
            source_kind=message.source_kind,
            recent_context=recent_context,
        )
        resolution = await resolve_routing_request(self.model, request)
        if resolution.outcome == "failed":
            LOG.warning("Telegram General router failed: %s", resolution.detail)
            self._audit(
                message,
                outcome="failed",
                detail=resolution.detail,
                payload=utterance,
            )
            await self._feedback(
                message, "The destination could not be resolved. Nothing was sent."
            )
            return False
        if resolution.outcome == "rejected":
            self._audit(
                message,
                outcome="rejected",
                detail=resolution.detail,
                payload=utterance,
            )
            await self._feedback(
                message,
                "A single destination could not be selected. Nothing was sent.\n"
                + candidate_summary(candidates),
            )
            return False

        selected = resolution.selected
        prompt = resolution.prompt
        assert selected is not None
        self._audit(
            message,
            outcome="succeeded",
            detail=resolution.detail,
            payload=utterance,
            target_key=selected.key.to_storage_key(),
        )
        claimed = await self.mutations.send_prompt(
            message,
            event_key=event_key,
            route=selected.route,
            text=prompt,
        )
        if claimed:
            self._save_context(message, selected)
            await self._feedback(
                message,
                f"→ {selected.route.name}\n{truncate(prompt, max_chars=300)}",
            )
        return claimed

    def clear_context(self, message) -> bool:
        if self.router.context_ttl_seconds <= 0:
            return False
        key = self._context_key(message)
        existed = self.store.get_state(key) is not None
        self.store.delete_state(key)
        if existed:
            self.store.add_transport_audit(
                TransportAuditEntry(
                    transport="telegram",
                    actor_id=str(message.user_id),
                    container_id=str(message.chat_id),
                    conversation_id=None,
                    action="route_context",
                    outcome="succeeded",
                    detail="recent General destination cleared",
                )
            )
        return existed

    def _recent_context(
        self,
        message,
        candidates: tuple[RouterCandidate, ...],
    ) -> RecentRoutingContext | None:
        ttl = self.router.context_ttl_seconds
        if ttl <= 0:
            return None
        key = self._context_key(message)
        value = self.store.get_state(key)
        if value is None:
            return None
        try:
            raw = json.loads(value)
            thread_id = int(raw["thread_id"])
            saved_at = float(raw["saved_at"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            self.store.delete_state(key)
            return None
        age = time.time() - saved_at
        if age < 0 or age > ttl:
            self.store.delete_state(key)
            return None
        candidate = next(
            (
                item
                for item in candidates
                if int(item.route.chat_id) == message.chat_id
                and int(item.route.thread_id) == thread_id
            ),
            None,
        )
        if candidate is None:
            self.store.delete_state(key)
            return None
        return RecentRoutingContext(candidate=candidate, age_seconds=int(age))

    def _save_context(self, message, candidate: RouterCandidate) -> None:
        if self.router.context_ttl_seconds <= 0:
            return
        self.store.set_state(
            self._context_key(message),
            json.dumps(
                {
                    "v": 1,
                    "thread_id": int(candidate.route.thread_id),
                    "saved_at": time.time(),
                },
                separators=(",", ":"),
            ),
        )

    @staticmethod
    def _context_key(message) -> str:
        return f"telegram:general-context:{message.chat_id}:{message.user_id}"

    async def _candidates(self) -> tuple[RouterCandidate, ...]:
        targets = await asyncio.to_thread(self.client.list_targets)
        candidates: list[RouterCandidate] = []
        for target in targets:
            if not target.agent_name:
                continue
            try:
                key = self.client.target_key(target.target)
            except Exception:
                continue
            route = self.store.get_telegram_topic_for_target(key)
            if route is None or route.closed:
                continue
            if route.route_target != key.to_route_value():
                self.store.upsert_telegram_topic_for_target(
                    target=key,
                    chat_id=route.chat_id,
                    thread_id=route.thread_id,
                    name=route.name,
                    closed=route.closed,
                )
                route = self.store.get_telegram_topic_for_target(key)
                if route is None:
                    continue
            candidates.append(
                RouterCandidate(
                    candidate_id=f"candidate-{len(candidates) + 1}",
                    key=key,
                    target=target,
                    route=route,
                )
            )
        return tuple(candidates)

    def _audit(
        self,
        message,
        *,
        outcome: str,
        detail: str,
        payload: str,
        target_key: str | None = None,
    ) -> None:
        self.store.add_transport_audit(
            TransportAuditEntry(
                transport="telegram",
                actor_id=str(message.user_id),
                container_id=str(message.chat_id),
                conversation_id=(
                    str(message.message_thread_id)
                    if message.message_thread_id is not None
                    else None
                ),
                action="route_text",
                target_key=target_key,
                payload_preview=truncate(payload, max_chars=200),
                outcome=outcome,
                detail=truncate(detail, max_chars=500),
            )
        )

    async def _feedback(self, message, text: str) -> None:
        await self.api.send_text(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text=text,
        )


def _pi_environment(config: RouterModelConfig) -> dict[str, str]:
    allowed = {
        "HOME",
        "USER",
        "LOGNAME",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    }
    allowed.update(config.pass_env)
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    resolved_cli = shutil.which(config.cli_path)
    path_entries = [
        os.path.dirname(resolved_cli or config.cli_path),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    if config.node_path:
        path_entries.insert(0, os.path.dirname(config.node_path))
    env["PATH"] = ":".join(dict.fromkeys(entry for entry in path_entries if entry))
    return env


async def _run_command(
    argv: tuple[str, ...],
    *,
    input_bytes: bytes | None,
    config: RouterModelConfig,
    label: str,
    env: dict[str, str] | None = None,
) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=(asyncio.subprocess.PIPE if input_bytes is not None else None),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        raise RouterError(f"{label} could not start: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input_bytes),
            timeout=config.timeout_seconds,
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RouterError(f"{label} timed out") from exc
    if len(stdout) > config.max_output_bytes:
        raise RouterError(f"{label} output exceeded the size limit")
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip()
        raise RouterError(
            f"{label} failed"
            + (f": {truncate(detail, max_chars=300)}" if detail else "")
        )
    return stdout


def _parse_decision(value: str, *, request: RoutingRequest) -> RoutingDecision:
    try:
        raw = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RouterError("Router model output was not JSON") from exc
    if not isinstance(raw, dict):
        raise RouterError("Router model output must be an object")
    intent = raw.get("intent")
    candidate_id = raw.get("candidate_id")
    prompt = raw.get("message_verbatim")
    ambiguous = raw.get("ambiguous")
    routing_basis = raw.get("routing_basis")
    reason = raw.get("reason")
    if intent not in {"send_prompt", "unknown"}:
        raise RouterError("Router model returned an unsupported intent")
    if candidate_id is not None and not isinstance(candidate_id, str):
        raise RouterError("Router candidate_id must be a string or null")
    valid_ids = {candidate.candidate_id for candidate in request.candidates}
    if candidate_id is not None and candidate_id not in valid_ids:
        raise RouterError("Router model invented an unknown candidate")
    if prompt is not None and not isinstance(prompt, str):
        raise RouterError("Router message_verbatim must be a string or null")
    if not isinstance(ambiguous, bool):
        raise RouterError("Router ambiguous must be boolean")
    if routing_basis not in {
        "explicit_destination",
        "recent_context",
        "unknown",
    }:
        raise RouterError("Router routing_basis is unsupported")
    if reason is not None and not isinstance(reason, str):
        raise RouterError("Router reason must be a string or null")
    return RoutingDecision(
        intent=intent,
        candidate_id=candidate_id,
        message_verbatim=prompt.strip() if isinstance(prompt, str) else None,
        ambiguous=ambiguous,
        reason=reason,
        routing_basis=routing_basis,
    )


def candidate_summary(candidates: tuple[RouterCandidate, ...]) -> str:
    lines = ["Candidate agents:"]
    for candidate in candidates[:8]:
        lines.append(
            f"{candidate.candidate_id}: {candidate.display_name} "
            f"({candidate.target.status or 'unknown'})"
        )
    return "\n".join(lines)


_SYSTEM_PROMPT = """You are a routing classifier for a local agent relay.
Return exactly one JSON object and no prose.
Choose only from candidate ids supplied by the caller. Never invent an id.
Extract message_verbatim as an exact contiguous substring of the utterance.
Remove only the words that identify the destination; never rewrite, summarize,
translate, correct, or expand the instruction itself.
A non-null recent_context identifies the user's last successfully routed General
destination. If the current utterance explicitly names a destination, prefer
that destination even when it differs from recent_context, and return
routing_basis=explicit_destination. If no destination is named and the utterance
is a plausible conversational continuation, select recent_context and return
routing_basis=recent_context. Do not use recent_context when it is null. Return
routing_basis=unknown whenever no unique destination is supported.
For source_kind=voice, the utterance is an automatic speech transcript. Spoken
letters, separators, Latin text, kana, and similar sounds may be mistranscribed.
Use the full candidate metadata to infer the intended destination when exactly
one candidate is clearly plausible; do not require a literal spelling match.
If multiple candidates remain similarly plausible, return unknown rather than
guessing. For source_kind=text, apply ordinary strict natural-language matching.
If neither an explicit destination nor a usable recent_context uniquely supports
a destination, return intent=unknown, candidate_id=null, message_verbatim=null,
ambiguous=true, routing_basis=unknown. Never choose based only on status or
candidate order.
"""
