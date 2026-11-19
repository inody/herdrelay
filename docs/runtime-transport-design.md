# Runtime and transport architecture

Status: proposed

This document defines the next HerdRelay architecture: Telegram and Discord are
independent chat transports, while Herdr Core, the legacy Herdr TUI, and Orca
are interchangeable runtime backends. It generalizes the earlier
[Herdr Core and Telegram architecture](core-telegram-design.md) without
changing the current production path by default.

The staged migration is described in
[Runtime and transport implementation plan](runtime-transport-plan.md).

## 1. Decision

HerdRelay will model runtime backends and chat transports as two independent
axes around one relay core.

```text
                         ┌─ DiscordTransport
                         │
HerdrCoreBackend ─┐      │
HerdrTuiBackend ──┼─> RelayCore
OrcaBackend ──────┘      │
                         └─ TelegramTransport
```

A deployment initially selects exactly one runtime backend and may enable one
or more transports. Supporting several runtime backends in one process is a
future extension, not an implicit consequence of this design.

Orca is a first-class backend, not an external delivery adapter. A thin Orca
connector may exist where Orca's public CLI does not expose a sufficiently
precise operation, but that connector remains an implementation detail of
`OrcaBackend`. It does not own routes, authorization, audit policy, delivery,
or transport behavior.

## 2. Goals

- Select `herdr-core`, `herdr-tui`, or `orca` through runtime configuration.
- Enable Telegram, Discord, or both without making either transport the process
  owner.
- Keep authorization, audit, rate limits, deduplication, routing, and mutation
  policy independent of runtime and transport libraries.
- Deliver one normalized backend event to every enabled destination without
  scanning or interpreting it separately in each transport.
- Preserve exact backend identity and never route by title, cwd, or display
  label guesses.
- Keep backend-specific capabilities explicit instead of emulating unsupported
  operations.
- Retain current production behavior during incremental migration.
- Use only documented Herdr and Orca interfaces; do not bind HerdRelay to an
  application's private database, internal daemon socket, or UI tree.

## 3. Non-goals

- Running several backends in one process in the first implementation.
- Making all backends support the legacy TUI's raw-key and pane-management
  operations.
- Providing terminal emulation or arbitrary shell access over chat.
- Replacing Orca, Herdr Core, or Herdr TUI session management.
- Moving Telegram- or Discord-specific controls into the relay domain.
- Requiring an Orca plugin when the public CLI is sufficient.

## 4. Process composition

A new application supervisor owns the process lifecycle.

```text
HerdRelayApplication
├─ BackendProvider
│  ├─ RuntimeBackend
│  └─ BackendEventSource
├─ RelayCore
├─ Store
├─ DiscordTransport, optional
└─ TelegramTransport, optional
```

The supervisor starts the backend provider, relay core, and all enabled
transports as peers. Discord must no longer create or stop Telegram as a child
service. A Discord disconnect must not stop Telegram, backend event ingestion,
or durable delivery tracking.

The same composition is used by the normal executable and tests. A
transport-specific executable may select a subset of transports, but it must
not construct a separate routing or mutation path.

## 5. Domain model

### 5.1 Target identity

A durable target key identifies a logical runtime endpoint and excludes mutable
presentation data.

```text
TargetKey
- version
- backend: herdr-core | herdr-tui | orca
- instance_id
- address
```

Interpretation by backend:

| Backend | `instance_id` | `address` |
|---|---|---|
| Herdr Core | stable Core identity | exact terminal address |
| Herdr TUI | configured session/socket identity | pane ID |
| Orca | Orca runtime/host identity | stable pane or terminal identity |

Titles, repository names, cwd, workspace labels, and agent names are display
metadata. They are never sufficient to bind or mutate a target.

Existing serialized Herdr target keys remain readable. A schema migration may
normalize them into the new representation, but no migration may infer an Orca
or Core identity from matching labels.

### 5.2 Target generation

A target key can outlive one agent conversation. Generation data therefore
belongs to snapshots and events rather than the durable key.

Examples:

- Herdr Core: terminal creation identity or observed turn metadata;
- Herdr TUI: detected agent/session generation when available; and
- Orca: `orcaSessionId`, which can change after `/clear` while the pane remains.

Generation data prevents a stale action or output event from being applied to a
new conversation in the same endpoint.

### 5.3 Target snapshot

A normalized target snapshot contains:

- target key and generation;
- lifecycle state;
- normalized agent state;
- agent application;
- display title and labels;
- current turn or backend revision when available;
- timestamps; and
- capability-relevant metadata.

Raw backend payloads may be retained for local diagnostics but are never sent
unchanged to a transport.

### 5.4 Backend event

Backend event kinds include:

- snapshot replacement;
- target created, updated, restarted, finished, or removed;
- status changed;
- response completed;
- structured question created or resolved;
- human-input visibility changed;
- backend disconnected; and
- backend resynchronized.

Every event carries a stable event ID or enough backend metadata for the
provider to construct one. Delivery deduplication remains per route, not merely
per event.

### 5.5 Inbound action

Transports normalize platform updates into actions such as:

- list targets;
- send text;
- interrupt;
- answer a structured question;
- show help or status; and
- select, bind, mute, or unmute a route.

An action carries actor, location, source-message identity, target key, expected
generation when available, and a bounded payload preview for audit.

## 6. Backend provider boundary

A backend provider groups command operations with the event sources needed to
observe that runtime.

```python
class RuntimeBackend(Protocol):
    capabilities: RuntimeCapabilities

    async def list_targets(self) -> list[TargetSnapshot]: ...
    async def get_target(self, key: TargetKey) -> TargetSnapshot: ...
    async def send_text(
        self,
        key: TargetKey,
        text: str,
        *,
        expected_generation: str | None,
    ) -> MutationResult: ...
    async def interrupt(
        self,
        key: TargetKey,
        *,
        expected_generation: str | None,
    ) -> MutationResult: ...


class BackendEventSource(Protocol):
    async def events(self) -> AsyncIterator[BackendEvent]: ...


@dataclass
class BackendProvider:
    runtime: RuntimeBackend
    events: BackendEventSource
```

The exact Python shape may change during implementation, but the boundary must
preserve these properties:

- the relay core sees normalized domain objects only;
- backend calls are async from the coordinator's perspective, even when a
  driver uses a blocking CLI internally;
- capabilities are checked before rendering or executing an action;
- timeout and unknown-outcome classification occur at the backend boundary;
- reconnect and snapshot replacement are backend responsibilities; and
- target identity validation happens again immediately before a mutation.

Legacy-only operations may remain in a compatibility extension, but they do not
belong to the common backend interface.

## 7. Relay core

`RelayCore` is the only component allowed to combine backend facts, transport
actors, routes, and mutation policy. It owns:

- target snapshot state;
- route reconciliation;
- authorization and feature flags;
- message length and text safety policy;
- mutation claims and audit completion;
- rate limits;
- expected-generation checks;
- event-to-route fan-out;
- per-route delivery deduplication;
- delivery retry classification; and
- transport-neutral controls and structured choices.

The core must not import `discord`, call the Telegram Bot API, execute `hc`,
execute `herdr`, or execute `orca` directly.

### 7.1 Outbound fan-out

One backend event is consumed once and converted into zero or more delivery
jobs.

```text
BackendEvent
  -> RelayCore policy and route lookup
  -> DeliveryJob[Discord]
  -> DeliveryJob[Telegram]
```

Each delivery has an independent durable key. A successful Discord post never
suppresses Telegram, and a Telegram failure never causes the backend event to
be reinterpreted.

### 7.2 Inbound mutations

For a mutating action, the core:

1. authenticates actor and location;
2. resolves the destination-bound route;
3. checks the requested backend capability;
4. refreshes or verifies the target snapshot;
5. checks target generation, state, blocklist, length, and rate limits;
6. atomically claims the mutation in the audit store;
7. calls the backend exactly once;
8. records `succeeded`, `rejected`, `failed`, or `outcome_unknown`; and
9. asks the originating transport to render feedback.

Transport feedback failure never replays the backend mutation.

## 8. Transport boundary

A transport translates between platform updates and normalized relay objects.

```python
class ChatTransport(Protocol):
    name: str
    capabilities: TransportCapabilities

    async def run(self, inbound: InboundActionSink) -> None: ...
    async def ensure_route(self, target: TargetSnapshot) -> Route: ...
    async def update_route(self, route: Route, target: TargetSnapshot) -> None: ...
    async def close_route(self, route: Route) -> None: ...
    async def deliver(self, job: DeliveryJob) -> DeliveryResult: ...
```

Discord owns Discord threads, reactions, mentions, and `discord.ui` controls.
Telegram owns forum topics, callbacks, reactions, and Bot API limits. Neither
transport lists runtime targets or performs runtime mutations directly.

## 9. Backend implementations

### 9.1 Herdr Core

`HerdrCoreBackend` keeps the existing direct, existing-socket JSON-RPC model.
It uses Core identity and exact terminal addresses, consumes native lifecycle
events, and permits only explicitly supported methods. Agent hook inbox events
are normalized by the Core provider and checked against the active Core
identity before entering the relay core.

### 9.2 Legacy Herdr TUI

`HerdrTuiBackend` wraps the existing `herdr` CLI and socket watcher. Legacy raw
input, approval, start, and close operations remain optional compatibility
capabilities and are not added to the common interface. Hook inbox version 1 is
owned by this provider during the compatibility period.

### 9.3 Orca

`OrcaBackend` uses documented Orca interfaces. The known public surface
includes:

- `orca agent list --json` for discovery and state;
- `orca agent read <session-id> --json` for conversation output;
- `orca terminal send` for accepted terminal input and interrupt-style input
  where a live terminal handle is available;
- `agent.status.changed` for plugin event-driven status updates; and
- plugin host APIs such as `agents.list` and `terminal.sendText`.

The backend must not read Orca's private application database or call its
private daemon socket.

#### Identity

The preferred durable identity is:

```text
instance_id = Orca runtime ID plus host identity
address     = Orca pane key or stable terminal ID
```

`orcaSessionId` is generation data, not the sole durable route key. Repository,
workspace, branch, title, and cwd are labels only.

#### Discovery and output

The first implementation may use bounded CLI polling if the public CLI does not
provide a production event subscription. It records `updatedAt`, session ID,
state transitions, and response identity so persistent `done` rows do not
produce duplicate output.

A later event-driven implementation may use an Orca plugin contribution for
`agent.status.changed`. The plugin should only expose typed runtime facts and
operations required by `OrcaBackend`.

#### Thin connector rule

A connector is allowed only when the public CLI cannot safely map an Orca
session to the exact terminal operation. It must:

- live under `integrations/orca/` initially;
- use a versioned local contract;
- expose target discovery, status, read, send, and interrupt only as needed;
- contain no Telegram or Discord code;
- contain no route, authorization, rate-limit, or audit policy; and
- fail closed when identity is ambiguous.

This keeps the product architecture one backend deep even if the backend has a
small platform driver.

## 10. Capabilities

Backend capabilities are data, not type checks spread through transports.
Candidate fields include:

- `list_targets`;
- `normal_send`;
- `bounded_read`;
- `native_events`;
- `completed_response`;
- `interrupt`;
- `structured_questions`;
- `raw_input`;
- `guarded_approval`;
- `agent_start`; and
- `target_close`.

The relay core rejects unsupported actions before calling a backend. Transports
render controls from capabilities and configuration, so an Orca or Core target
never receives a legacy-only approval button accidentally.

## 11. Configuration

The existing top-level grouping remains suitable:

```yaml
runtime:
  provider: orca                 # herdr-core, herdr-tui, or orca
  herdr_core:
    socket_path: null
  herdr_tui:
    cli_path: herdr
  orca:
    cli_path: /Applications/Orca.app/Contents/Resources/bin/orca
    environment: local
    include_terminal_agents: true
    include_chats: false
    refresh_seconds: 2

transports:
  discord:
    enabled: true
  telegram:
    enabled: true
```

Only the selected provider's required settings are validated. Unsupported
feature flags fail at startup rather than disappearing silently.

The first release keeps one active backend per process. A future multi-backend
configuration would require named backend instances and backend-qualified route
selection; it must not overload this single-provider schema implicitly.

## 12. Storage

The generic schema should converge on:

- `targets`: durable target key and last snapshot;
- `routes`: target-to-transport destinations;
- `deliveries`: event/route delivery state;
- `transport_audit`: mutation claims and outcomes;
- `backend_state`: provider cursors, revisions, and reconnect state; and
- `pending_actions`: structured questions and confirmation state.

Existing Discord bindings and Telegram topics remain readable during migration.
Backend-specific columns may be retained for compatibility, but new code uses a
versioned serialized target key.

Orca cursors must persist enough information to avoid replaying every completed
agent after restart. At minimum this includes target key, session generation,
last observed update, and last delivered response identity.

## 13. Failure model

- Backend disconnection is reported once and retried with bounded backoff.
- A new authoritative snapshot replaces stale in-memory backend state.
- Malformed snapshots never trigger mass route closure.
- Delivery failure is per transport destination.
- Mutation timeout after submission becomes `outcome_unknown` and is not
  retried automatically.
- Ambiguous target identity is a rejection, not a best-effort match.
- A transport outage does not stop backend ingestion or another transport.
- A backend outage does not prevent read-only transport commands such as help
  from responding.

## 14. Security boundary

Every enabled transport remains remote access to the selected backend.

- Target inclusion is deny-by-default for each backend.
- Orca targets require explicit host/workspace/project or exact-target policy;
  discovery of an agent does not automatically authorize it.
- Chat actors and locations are checked before backend reads that could expose
  private metadata.
- Backend drivers expose a narrow method set and never arbitrary CLI arguments.
- Raw terminal content is bounded and never included unless a configured event
  requires it.
- Output events and local connector files use private directories and atomic
  writes.
- External platform feedback cannot cause a mutation replay.

## 15. Repository layout

The intended layout is:

```text
src/herdrelay/
  app.py
  domain/
  relay/
  backends/
    base.py
    herdr_core.py
    herdr_tui.py
    orca.py
  transports/
    base.py
    discord.py
    telegram.py
  store/
integrations/
  agents/
  orca/                    # only if a thin Orca connector is required
docs/
```

This is a direction, not a requirement for one large file move. Refactoring
should proceed behind compatibility imports and small reviewed changes.

The Orca backend and its initial connector remain in this repository because
they share HerdRelay's target, mutation, and event contracts. Extraction into a
separate repository is appropriate only if the connector becomes useful without
HerdRelay or needs an independent release lifecycle.

## 16. Compatibility and rollout

- Existing `runtime.provider: herdr-core` and `herdr-tui` configurations retain
  their behavior until explicitly migrated.
- Current event versions 1 and 2 remain readable during the refactor.
- Orca introduces a new normalized identity rather than pretending to be a
  Herdr target.
- Existing route records are never rebound to Orca by cwd, repository, title,
  or agent-name similarity.
- Database changes are additive before legacy columns are retired.
- The supported production default remains Herdr Core until Orca has completed
  read-only and mutation acceptance tests.

## 17. Resolved design questions

- **Is Orca an adapter?** No. It is a runtime backend. Any plugin or CLI wrapper
  is an internal driver.
- **Should the Orca integration begin in another repository?** No. Keep it here
  until its boundary and reuse case are proven.
- **Can Telegram and Discord run together?** Yes. They are peer transports and
  receive independent delivery records.
- **Can Herdr Core, TUI, and Orca run together in one process?** Not initially.
  One selected backend keeps routing and policy understandable. Named
  multi-backend support requires a separate design.
- **Where do output hooks belong?** Inside the selected backend provider's event
  composition, not inside a chat transport.
- **Can the public Orca CLI map an agent to an exact writable terminal?** Yes.
  The implementation joins agent `paneKey` to terminal `tabId:leafId` and also
  requires host, worktree, and agent identity to agree uniquely.

## 18. Open implementation questions

These questions require characterization tests rather than assumptions:

- Which documented Orca operation can semantically stop a recognized agent
  turn. `terminal send --interrupt` accepted one byte in live characterization,
  but the tested Pi agent remained `working`.
- Whether `agent.status.changed` covers native chats and terminal agents with
  the same identity guarantees.
- How Orca reports a completed response after restart or while a workspace is
  asleep.
- Which Orca identity remains stable across tab restore, `/clear`, workspace
  sleep, app restart, and remote environments.
- Whether structured agent questions can be read through a documented Orca
  interface without scraping terminal output.

The Orca rollout remains experimental despite successful Telegram send/output
acceptance; Discord acceptance and broader operational soak remain pending. It
continues to fail closed on ambiguous identity or unverified interrupt
semantics.
