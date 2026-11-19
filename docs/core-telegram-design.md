# Herdr Core and Telegram architecture

Status: implemented foundation; retained as the Core/Telegram-specific design

The generalized backend/transport architecture, including Orca as a first-class
runtime backend, is defined in
[Runtime and transport architecture](runtime-transport-design.md).

This document defines the target architecture for migrating HerdRelay from the
legacy Herdr TUI to Herdr Core and for supporting Telegram alongside Discord.
It describes intended behavior, not functionality available in the current
release. The staged implementation is in
[Core and Telegram implementation plan](core-telegram-plan.md).

## 1. Purpose

HerdRelay currently binds Discord threads directly to panes in the legacy Herdr
TUI. Pane discovery, input, status events, thread lifecycle, and some control
logic all depend on the legacy `herdr` CLI and socket protocol. Discord types
also appear in the orchestration layer, which makes a second chat transport
more expensive and error-prone than it should be.

The target design has two independent axes:

1. **Runtime backend** — where terminals and agents live. Herdr Core becomes the
   primary backend; the legacy Herdr TUI remains a temporary compatibility
   backend during migration.
2. **Chat transport** — where a person receives updates and sends actions.
   Discord and Telegram become adapters over one shared relay domain.

Herdr app is a view over Herdr Core. HerdRelay must not require the app window
to be open for ordinary discovery, status, input, output, or interruption.
App integration is optional and limited to operations that explicitly need a
visible app space, tab, or pane.

## 2. Goals

- Use Herdr Core as the source of truth for terminals, agent status, turn
  progression, lifecycle, and machine identity.
- Keep the current Discord integration working while runtime and transport
  boundaries are introduced.
- Add Telegram as a first-class transport rather than a fork of the Discord
  bot.
- Support Apple Watch notification and short-reply workflows through the
  official Telegram client.
- Keep all Core access local. Neither Discord nor Telegram receives the raw
  Core protocol or a shell-equivalent API.
- Preserve allowlists, auditing, deduplication, bounded message sizes, and
  failure visibility across transports.
- Allow future transports to reuse the same runtime and policy layers.
- Align status and action semantics with Herdr Wear without making the public
  HerdRelay repository depend on a private repository.

## 3. Non-goals for the first Core release

- Terminal emulation or a complete remote screen.
- Exposing `hc connect`, the Core socket, or arbitrary Core methods over the
  network.
- Feature parity with every legacy TUI control.
- Arbitrary key injection from Telegram or Discord.
- Approval or denial buttons implemented by unguarded `terminal.keys` calls.
- Starting, killing, removing, or moving terminals from chat.
- Managing Herdr app layouts or focus as part of routine relay operation.
- Replacing the native Herdr Wear Apple Watch client.

## 4. Key decisions

### 4.1 Herdr Core is the runtime authority

The Core backend addresses terminals, not app panes. A target is identified by
Core identity and terminal address. For a local terminal this may look like
`t12`; a terminal reached through a connected machine may look like
`machine/t12`.

App labels such as `herdr.space`, `herdr.tab`, and `where` may be used for
presentation and routing policy. They are not the durable target identity.

### 4.2 Herdr app is optional presentation metadata

The bridge reads Core facts and events. It does not scrape the Herdr app or
follow its focused pane. App commands are reserved for a future, explicitly
configured capability that creates a visible terminal. A background chat
service must remain functional when the app window is closed.

### 4.3 Chat transports do not contain relay policy

Discord and Telegram adapters translate platform updates into normalized
inbound actions and render normalized outbound events. Runtime selection,
authorization, audit decisions, deduplication, target lookup, and mutation
policy live above the transport adapters.

### 4.4 Telegram uses an outbound-only bot connection

The default Telegram connection is long polling. It requires no inbound port,
public webhook, or Tailscale exposure. The bot communicates with Telegram over
outbound HTTPS, as the Discord adapter communicates with the Discord gateway.

### 4.5 Telegram forum topics are the preferred conversation model

The preferred deployment is a private forum-enabled supergroup with one topic
per normalized runtime target: a legacy TUI pane or a Core-ID-pinned terminal.
A topic is the Telegram equivalent of a Discord pane thread. A direct-message
mode may be added as a fallback if Apple Watch topic behavior is inadequate on
supported devices.

### 4.6 Runtime mutations remain deliberately narrow

Telegram permits normal prompt submission to either runtime after an atomic
claim and audit write. Core additionally permits confirmation-gated
`terminal.interrupt`; legacy TUI interrupt is not exposed through Telegram.
Core does not expose raw keys or force-send behavior. Mutation timeouts produce
an unknown outcome and are never retried automatically.

### 4.7 Herdr Wear remains a separate client path

Herdr Wear provides a dedicated Gateway, iPhone companion, and native Watch
experience. Telegram provides a faster cloud messaging path for notifications
and short replies. The two products should align their domain vocabulary and
safety rules, but neither should silently weaken the other's trust boundary.

## 5. Target architecture

```text
Claude/Codex hooks ─┐
Pi extension ───────┴─> normalized local inbox ───────────────┐
                                                               │
Herdr Core ─> CoreBackend ─> RelayCoordinator ─> Policy/Audit ├─> DiscordTransport
     │             │                    │                      └─> TelegramTransport
     │             │                    │
     │             └─ events/snapshots  └─ Store: targets, routes, deliveries
     │
     └─ optional Herdr app labels for display names and visibility policy
```

The current legacy backend fits behind the same interface during migration:

```text
Legacy Herdr TUI ─> LegacyHerdrBackend ─> RelayCoordinator
Herdr Core        ─> HcBackend          ─> RelayCoordinator
```

Only one runtime backend is active for a deployment unless a later design adds
explicit multi-runtime support. Target identifiers from different runtimes must
never be guessed or merged.

## 6. Domain model

### 6.1 Target key

A durable target key contains:

- `runtime`: `herdr-core` or `herdr-tui`;
- `controller_core_id`: the Core to which HerdRelay is connected;
- `origin_core_id`: the Core that owns the terminal, when available; and
- `address`: the owning Core's terminal address or a legacy pane ID.

A serialized key must be unambiguous and versioned. Prefer
`origin_core_id + address` for Core identity. If the CLI exposes only a
controller-relative qualified address such as `machine/t12`, store that full
qualified address with `controller_core_id` and treat a machine rename as an
identity migration that requires explicit reconciliation. Display names, cwd,
titles, space IDs, tab IDs, and machine aliases are mutable metadata and are not
otherwise part of the identity.

### 6.2 Target snapshot

A normalized target snapshot contains only fields required by relay behavior:

- target key;
- live or finished lifecycle state;
- agent application, when reported;
- normalized status: `idle`, `working`, `done`, `blocked`, `error`, or unknown;
- status message, title, and turn;
- cwd and foreground process summary;
- display labels such as host, space, tab, and `where`;
- creation, status-change, and finish timestamps when available; and
- raw backend data retained only for diagnostics, never sent directly to a
  chat transport.

The coordinator filters finished records and non-agent shells according to an
explicit inclusion policy. The default Core policy should expose live,
reported coding-agent terminals that are visible in Herdr app. Hidden workers
and unreported shells require opt-in policy.

### 6.3 Runtime event

Normalized runtime events include:

- snapshot replacement;
- target created, updated, restarted, finished, or removed;
- status transition;
- turn transition;
- title, labels, foreground, or size change; and
- backend disconnected or resynchronized.

Events carry a backend sequence or revision when available. A fresh snapshot
replaces prior state after reconnect or a missed-event notification.

### 6.4 Adapter output event

Agent adapters write versioned local events independent of chat transport:

```json
{
  "version": 2,
  "event_id": "stable-event-id",
  "runtime": "herdr-core",
  "core_id": "core-identity",
  "target": "t12",
  "agent": "pi",
  "kind": "response",
  "text": "completed assistant response"
}
```

Supported kinds remain conceptually equivalent to the current inbox:
`response`, `question`, `question_resolved`, and `human_input`. Version 2 must
not store human prompt text for `human_input` events.

During migration, the reader accepts both the current pane-based version 1 and
the Core-aware version 2. New adapters prefer `HC_TERMINAL_ID` and Core
identity when available, then fall back to the legacy pane environment.

### 6.5 Route

A route binds one normalized target to one transport destination.

Common fields:

- target key;
- transport kind;
- transport account or bot identity;
- destination container;
- conversation or topic identifier;
- enabled/muted/closed state;
- creation and last-delivery timestamps; and
- optional transport metadata.

Discord-specific guild, channel, and thread IDs and Telegram-specific chat and
`message_thread_id` values stay in transport metadata or typed columns. The
coordinator sees only the normalized route.

### 6.6 Inbound action

Every inbound transport update becomes one of a small number of actions:

- send text;
- interrupt;
- list targets;
- select or bind target;
- read help or status; and
- future structured action answer.

An action carries normalized actor and location identities, the target key,
transport message identity, and payload preview for auditing.

## 7. Runtime backend interface

The initial interface should cover only behavior required by current relay
features:

```text
list_targets() -> snapshots
get_target(target_key) -> snapshot
subscribe_events() -> snapshots and runtime events
read_tail(target_key, lines) -> text
send_prompt(target_key, text) -> mutation result
interrupt(target_key) -> mutation result
```

Legacy-only methods such as arbitrary keys, pane split, agent start, and pane
close do not belong in this minimum interface. Optional capabilities can be
advertised separately rather than forcing every backend to emulate unsafe
behavior.

### 7.1 Core backend transport

The runtime backend connects directly to the existing local Core Unix socket
using generation-1 plain JSON-RPC. It performs `hello` on every new connection
and uses only the required narrow method set:

- `terminal.list` and `terminal.get` for snapshots;
- `events.subscribe` for a snapshot followed by ordered changes;
- `terminal.read` for bounded terminal text;
- `terminal.send` with `force: false` for normal prompt submission; and
- `terminal.interrupt` for interruption.

The socket is resolved from an explicit setting or `HC_SOCKET`, then the
session's `core.lock` and normal XDG state/runtime locations. A failed socket
connection is an error. The bridge does not invoke `hc server status` or other
runtime CLI commands because the installed CLI may start Core as a side effect.
Only the separate static compatibility probe invokes `hc --version` and
`hc schema`; those commands do not contact or start Core.

The backend reconnects after event-stream exit, replaces state from the next
snapshot, tolerates unknown optional fields, and stops on an unsupported
protocol generation or missing required capability. The raw socket and
arbitrary RPC methods remain internal and are never exposed through a network
API or chat command.

### 7.2 Target inclusion policy

A Core can contain app-visible terminals, hidden task workers, plain shells,
finished history, and connected-machine terminals. The default policy is:

- include only exact terminal addresses in an explicit allowlist, with an empty
  allowlist exposing no Core targets;
- include only `state == live`;
- require a recognized reported agent application;
- require app visibility labels by default;
- exclude child workers unless explicitly enabled; and
- permit connected-machine targets only when the machine is connected and an
  allowlist permits it.

These rules must be configurable and covered by fixtures. A false negative is
safer than exposing an unrelated shell or hidden worker.

## 8. Relay coordinator

The coordinator owns behavior shared by transports:

- maintain the current normalized target snapshot;
- reconcile target routes;
- authorize actor, location, action, and target;
- enforce text length and blocklist policy;
- map adapter events to routes;
- deduplicate per event and route;
- record mutation attempts and outcomes;
- fan out status and response events to enabled routes;
- mark routes closed when the target finishes or disappears; and
- distinguish delivery failure from runtime mutation failure.

The coordinator must not import `discord` or a Telegram library. Rich controls
are represented as transport-neutral actions and choices. Each transport
renders only the controls it supports.

## 9. Transport interface

A transport adapter provides behavior similar to:

```text
start() / stop()
ensure_conversation(target, display_name) -> route
rename_conversation(route, display_name)
close_conversation(route)
send_text(route, text, actions=[])
send_status(route, status_event, actions=[])
normalize_inbound(platform_update) -> inbound action or ignore
```

Transport capabilities are explicit. For example, Discord and Telegram both
support conversations and buttons, but their limits, permissions, and callback
identifiers differ.

One transport failure must not stop the runtime watcher or another transport.
Delivery state is recorded per route so a partial multi-transport failure is
visible and retry policy can remain conservative.

## 10. Telegram design

The feasibility basis is the official Telegram Bot API support for forum topics
and `message_thread_id`, together with the Watch target, compose flow, bot
commands, and reply-markup code in the official Telegram iOS repository.
Source and metadata support do not establish physical-device reliability;
Phase 8 of the implementation plan remains a release gate.

### 10.1 Deployment model

Recommended deployment:

- a dedicated bot token;
- one private forum-enabled supergroup;
- the bot is an administrator with only the permissions required to manage and
  post in topics;
- exact Telegram user and chat ID allowlists;
- long polling rather than a webhook; and
- one topic per included Core target.

Normal messages in a bound topic are submitted to that target. Messages outside
the configured chat, from an unlisted user, from another bot, or in an unbound
topic are ignored or rejected before Core state is read.

### 10.2 Topic lifecycle

- Create a topic only after a target passes the inclusion and visibility
  policy.
- Prefer a name derived from host, `where`, agent application, and terminal
  address, within Telegram limits.
- Rename on meaningful location or title changes, with rate limiting.
- Close the topic after a confirmed finished or removed target.
- Reopen the same topic when the same Core target restarts and the platform
  permits it.
- Do not automatically bind an old legacy pane topic to a new Core terminal by
  matching cwd or title.

### 10.3 Watch-oriented message policy

Telegram messages intended for Apple Watch should be short and actionable.
Recommended defaults:

- blocked notification: target, state, short status message, no terminal tail;
- done notification: target and short completion summary;
- full assistant response: enabled separately and chunked below Telegram's
  message limit;
- plain text by default to avoid Markdown escaping failures; and
- no automatic link previews.

A deployment may choose `status`, `summary`, or `full` notification detail.
`status` should be the safe default.

### 10.4 Direct-message fallback

If forum topics are not usable enough on Apple Watch, direct-message mode uses:

- `/agents` to list targets;
- `/use <target>` to select the active target;
- reply-to-message metadata to route replies when available; and
- explicit target prefixes when no reliable reply context exists.

Direct-message mode must never guess among multiple targets.

### 10.5 Telegram controls

The Core MVP supports:

- list/status;
- send text;
- interrupt with confirmation; and
- mute/unmute notifications.

Inline keyboards may be rendered for these operations, but text-command
fallbacks remain available until physical Watch behavior is verified.

## 11. Discord compatibility

Discord remains supported throughout migration.

- Existing public thread behavior continues under the legacy backend.
- After the Core backend is enabled, a Discord thread binds to a Core target
  rather than a legacy pane.
- Current thread IDs and audit history remain in the database.
- Legacy pane bindings are not automatically converted into Core bindings.
- The Discord adapter owns `discord.ui.View`, reactions, mentions, and thread
  API calls.
- Structured question controls must be represented by transport-neutral choices
  before Telegram can render them.

The migration must not silently change current deployments from legacy TUI to
Core. Runtime selection is an explicit configuration change.

## 12. Mutation safety

### 12.1 Text submission

Before submission, the coordinator verifies:

- transport actor and location allowlists;
- route and target identity;
- target is live and recognized as an agent;
- message length and blocklist;
- target is not blocked; and
- backend capability is available.

Core `terminal.send` provides draft-aware delivery and refuses blocked programs
unless forced. HerdRelay never sets `force` in the initial implementation.
Because Core does not accept an expected turn for normal send, a concurrent
local state change can still race the preflight check. The audit record must
include the observed turn and returned mutation result.

### 12.2 Interrupt

Use `terminal.interrupt`, not a raw Ctrl-C key sequence. Require an authorized
user and an explicit configuration flag. Telegram should require a confirmation
callback or command before the mutation.

### 12.3 Approval and denial

The Core MVP disables approval and denial. Current Core key input cannot
atomically require the previously observed turn, status, status timestamp, and
prompt identity. A stale button could therefore answer a different prompt.

A future implementation requires either:

- an upstream guarded action API; or
- an opaque, adapter-produced action ID whose validity can be rechecked and
  consumed atomically without accepting arbitrary client keys.

Until then, blocked actions must be resolved in the terminal or another client
with an appropriate safety boundary.

### 12.4 Unknown mutation outcomes

A timeout or lost response after a mutation does not prove failure. HerdRelay:

- records `outcome_unknown`;
- does not retry automatically;
- tells the user to inspect current target state; and
- deduplicates explicit retries only when a backend-supported idempotency key
  exists.

## 13. Storage design

New generic tables should be added rather than rewriting legacy tables in
place. A candidate structure is:

- `targets`: versioned normalized target keys and last snapshot metadata;
- `routes`: target-to-transport destinations;
- `deliveries`: event/route delivery state and error summary;
- `runtime_events`: optional bounded transition checkpoints;
- existing `audit_log`: extended with runtime, transport, actor, observed turn,
  and mutation outcome; and
- existing `pending_questions`: migrated to normalized target keys.

Legacy `bindings` and `agent_threads` remain readable while the legacy backend
is supported. Schema migration must be transactional and idempotent.

Discord and Telegram may both route the same target. Delivery deduplication is
per route, not global, so one successful transport does not hide a failure on
another.

## 14. Configuration shape

The eventual configuration should separate runtime and transports. The exact
schema will be finalized during implementation, but the intended structure is:

```yaml
runtime:
  provider: herdr-core
  core:
    socket_path: null
    connect_timeout_seconds: 5
    request_timeout_seconds: 20
    event_idle_timeout_seconds: 35
    allowed_terminal_addresses: []
    include_hidden_workers: false
    include_connected_machines: false

transports:
  discord:
    enabled: true
    token_env: DISCORD_TOKEN
    # Discord allowlists and parent channel

  telegram:
    enabled: false
    token_env: TELEGRAM_BOT_TOKEN
    mode: forum
    allowed_chat_ids: []
    allowed_user_ids: []
    forum_chat_id: null
    statuses: [blocked, done]
    enable_send: false
    enable_interrupt: false
    confirmation_ttl_seconds: 60
    actor_mutations_per_minute: 6
    chat_mutations_per_minute: 12
    target_mutations_per_minute: 6
    notification_detail: status
```

Existing top-level Discord settings remain accepted during a deprecation
period. Configuration conversion must be explicit and documented.

## 15. Security boundary

Treat every enabled chat destination as remote terminal access.

- Core and its socket remain local to the host account.
- Bot tokens are secrets and are never stored in the database or repository.
- Telegram bots cannot use Secret Chats; Telegram messages are cloud-stored.
- Discord and Telegram content may contain source code or credentials. Default
  notifications minimize terminal and assistant text.
- Exact user and location allowlists are required for mutations.
- Raw Core methods, arbitrary keys, file APIs, machine APIs, and server control
  are not exposed.
- Adapter inbox files retain private directory and file modes.
- Audit previews remain bounded and must avoid storing human prompt text from
  visibility-only events.
- Browser-facing or remotely reachable HTTP APIs are outside this service's
  design.

## 16. Relationship to Herdr Wear

Herdr Wear and HerdRelay solve related but different delivery problems:

- HerdRelay uses third-party chat networks for immediate messaging and short
  remote actions.
- Herdr Wear uses a restricted host Gateway and native clients for a dedicated
  wearable experience.

They should align on:

- normalized agent states;
- target title and location fallbacks;
- reconnect and fresh-snapshot semantics;
- stale-action rejection;
- no automatic mutation retries;
- status-first notification defaults; and
- prohibition of raw Core API exposure.

HerdRelay should not import code from a private Herdr Wear repository. Shared
code can be extracted later only if it has a clear public home and stable API.
Until then, each project keeps independent contract tests against the same
published Core schema.

## 17. Compatibility and observability

The Core backend records and exposes for diagnostics:

- `hc` CLI and Core versions;
- Core ID and protocol generation;
- required method availability;
- event connection and reconnect count;
- latest snapshot sequence;
- target and route counts;
- last Discord and Telegram delivery errors; and
- mutation outcomes without secret payloads.

Contract tests use isolated `HC_SESSION`, HOME/XDG directories, and synthetic
status reporters. Tests must never type into or stop a production terminal.

## 18. Open decisions

The implementation plan can begin without resolving all of these, but each
must be decided before the corresponding feature ships:

1. Whether Core topics include only app-visible terminals or also explicitly
   labeled hidden terminals.
2. Whether connected-machine terminals are enabled in the first release.
3. Whether Telegram forum topics work well enough on the supported Apple Watch
   and Telegram versions.
4. Whether one process hosts both chat transports or transport processes are
   isolated behind a local event service. The initial preference is one asyncio
   process with independent transport tasks.
5. Whether to use `python-telegram-bot` or a smaller direct Bot API client. The
   initial preference is `python-telegram-bot` because it supports async long
   polling, forum topics, callbacks, and current Bot API types.
6. When legacy TUI support can be removed after Core migration has soaked in
   production.
