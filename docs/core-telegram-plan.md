# Herdr Core and Telegram implementation plan

Status: proposed

This plan implements the architecture in
[Herdr Core and Telegram architecture](core-telegram-design.md). It is organized
as reviewable phases that preserve the current Discord/legacy Herdr behavior
until a deployment explicitly selects the Core backend.

## 1. Delivery principles

- Keep `main` usable after every phase.
- Separate structural refactoring from behavior changes.
- Add Core and Telegram behind disabled configuration flags.
- Do not infer a Core target from a legacy pane ID, cwd, title, or app focus.
- Test Core integration in an isolated `HC_SESSION`; never use production
  terminals for integration input.
- Treat mutation timeout as an unknown outcome and never retry automatically.
- Do not add Core approval/denial through raw key injection.
- Keep commits and pull requests small enough to review by boundary: domain,
  backend, store, adapter, transport, then rollout.

## 2. Supported feature matrix

The first Core/Telegram release intentionally has a smaller mutation surface
than the legacy Discord backend.

| Capability | Legacy Discord | Core Discord MVP | Core Telegram MVP |
|---|---:|---:|---:|
| List detected agents | Yes | Yes | Yes |
| Per-target conversation | Yes | Yes | Yes, forum topic preferred |
| Blocked/done notification | Yes | Yes | Yes |
| Completed-response relay | Yes | Yes | Yes |
| Human-input visibility event | Pi | Yes | Yes if required by topic policy |
| Send normal prompt | Yes | Yes | Yes |
| Interrupt working turn | Yes | Yes | Yes, with confirmation |
| Raw special keys | Yes | No | No |
| Approve/deny buttons | Yes | No | No |
| Structured question answer | Claude-specific | Deferred | Deferred |
| Start visible agent | Yes | Deferred | Deferred |
| Close/kill terminal | Yes | Deferred | Deferred |
| Connected machines | No | Optional after local MVP | Optional after local MVP |

## 3. Phase 0 — Baseline and contracts

### Scope

Establish tests and fixtures before changing production structure.

### Work

- Record the currently supported legacy behavior in tests.
- Add normalized fixtures for representative Herdr Core terminal snapshots:
  - live Pi/Claude/Codex agent;
  - reported idle, working, done, blocked, and error states;
  - app-visible terminal labels;
  - unreported shell;
  - hidden child worker;
  - finished terminal;
  - connected-machine address; and
  - unknown optional fields.
- Add event fixtures for snapshot, status, turn, labels, title, finished,
  removed, heartbeat, missed/resnapshot, and unknown future events.
- Add a checked compatibility probe that records:
  - `hc --version`;
  - protocol generation;
  - required methods; and
  - JSON field assumptions used by the backend.
- Document how isolated Core tests create scratch HOME/XDG paths and a unique
  `HC_SESSION`.

### Tests

- Fixture decoding and unknown-field tolerance.
- No production Core or terminal is contacted by default test commands.
- Existing 238 Python tests and direct Pi adapter tests remain green.

### Exit criteria

- Current legacy behavior has regression coverage.
- Core fixtures cover every target inclusion and exclusion rule.
- The required Core capabilities are stated in one place.

## 4. Phase 1 — Introduce transport-neutral domain types

### Scope

Create domain types and protocols without changing runtime behavior.

### Work

- Add a package area such as `src/herdr_discord_bridge/domain/` or begin the
  gradual package rename to `src/herdrelay/` only if it can be done separately
  without obscuring functional changes.
- Define:
  - `TargetKey`;
  - `TargetSnapshot`;
  - `RuntimeEvent`;
  - `RelayOutputEvent`;
  - `Route`;
  - `Actor` and `Location`;
  - `InboundAction`; and
  - `MutationResult` with explicit success, rejected, failed, and
    outcome-unknown states.
- Define `RuntimeBackend` and transport protocols.
- Add adapters that translate the existing `HerdrTarget` and Discord locations
  into normalized types.
- Keep Discord UI classes inside the Discord adapter boundary.

### Tests

- Target key serialization round trips.
- Different runtime/Core identities never compare equal.
- Domain modules import neither `discord` nor a Telegram library.
- Legacy behavior remains unchanged through compatibility adapters.

### Exit criteria

- Coordinator-facing code can refer to targets and routes without a Discord or
  legacy pane type.
- No configuration migration is required yet.

## 5. Phase 2 — Generalize storage and auditing

### Scope

Add generic route and delivery storage while retaining legacy tables.

### Work

- Add transactional, idempotent schema migrations.
- Add candidate tables:
  - `targets`;
  - `routes`;
  - `deliveries`; and
  - optional bounded runtime checkpoints.
- Extend audit records with:
  - runtime;
  - transport;
  - normalized actor/location;
  - normalized target key;
  - observed turn/status; and
  - mutation outcome.
- Add normalized pending-question keys without deleting existing pane-based
  rows.
- Build a compatibility view or adapter over current `bindings` and
  `agent_threads` for the legacy backend.
- Make deduplication per event and route.

### Migration policy

- Preserve all current Discord mappings and audits.
- Do not create Core routes from legacy mappings automatically.
- Do not remove legacy tables while the legacy backend is supported.
- Back up or fail the migration before starting chat transports if schema
  conversion cannot complete atomically.

### Tests

- Fresh database creation.
- Upgrade from the current schema.
- Repeated migration is a no-op.
- Discord and Telegram routes for the same target have independent delivery
  state.
- Failed migration leaves the previous database usable.

### Exit criteria

- Generic routes can coexist with all existing data.
- Current deployments start with no user-visible behavior change.

## 6. Phase 3 — Refactor Discord into a transport adapter

### Scope

Move platform behavior behind `DiscordTransport` while using the legacy runtime
backend.

### Work

- Extract Discord-specific responsibilities:
  - bot lifecycle;
  - channel/thread lookup;
  - thread creation, rename, archive, and membership;
  - reactions;
  - mentions;
  - `discord.ui.View` rendering; and
  - message normalization.
- Move authorization inputs into normalized actor and location types.
- Convert watcher and stream output into transport-neutral outbound events.
- Replace direct Discord sends in runtime orchestration with transport calls.
- Keep current configuration keys and thread behavior intact.

### Tests

- Existing Discord tests continue to pass.
- Domain/coordinator tests use fake transports without importing Discord.
- A Discord HTTP failure does not stop runtime watching or inbox draining.
- Multi-chunk delivery state remains explicit.

### Exit criteria

- A second transport can be added without importing or calling Discord code.
- Production behavior under the legacy backend matches the current release.

## 7. Phase 4 — Add the Herdr Core backend

### Scope

Implement local Core discovery, events, reads, normal sends, and interrupts.

### Work

- Add configuration for explicit runtime selection:
  `herdr-tui` or `herdr-core`.
- Implement an `HcCommandClient` for bounded JSON commands.
- Implement an event process around `hc events --json`:
  - parse the initial snapshot;
  - filter live targets;
  - apply ordered updates;
  - handle heartbeat;
  - replace state after `missed` or reconnect;
  - tolerate unknown optional fields and future event kinds; and
  - expose connection state.
- Before launching the event process, confirm an existing Core is available.
  Keep `start_if_missing` false and unsupported in the first release.
- Negotiate or verify protocol generation and required methods.
- Implement target inclusion policy:
  - recognized reported agent;
  - app-visible by default;
  - hidden workers off by default;
  - connected machines off by default.
- Implement:
  - list/get;
  - bounded read;
  - send without `force`;
  - interrupt; and
  - mutation result classification.
- Derive display names from host/machine, `where`, title, app, and address.

### Tests

- Unit tests against all Phase 0 fixtures.
- Reconnect and fresh-snapshot replacement.
- Event sequence gaps and unknown events.
- Command timeout and malformed JSON classification.
- `terminal.send` blocked rejection.
- Interrupt uses `hc interrupt`, not raw keys.
- Isolated contract test using scratch HOME/XDG and `HC_SESSION`.
- Synthetic OSC status reporter exercises lifecycle transitions.
- Core restart does not terminate the bridge test's synthetic terminal unless
  the test explicitly owns and cleans it.

### Exit criteria

- Core Discord can list, bind, notify, relay output, send text, and interrupt in
  an isolated test deployment.
- No app window is required.
- No raw Core socket is exposed.

## 8. Phase 5 — Update Pi, Claude, and Codex adapters

### Scope

Make output events identify Core terminals while preserving legacy support.

### Work

- Update the Pi extension to prefer `HC_TERMINAL_ID` and Core/session identity,
  while retaining legacy pane fields when running under the TUI.
- Update Claude and Codex hooks similarly.
- Define inbox event version 2 and its validation limits.
- Continue accepting version 1 until legacy support is removed.
- Preserve Pi's rule that only interactive TUI human input emits
  `human_input`; RPC, SDK, extension, subagent, retry, and continuation activity
  must not expose a conversation.
- Ensure visibility events contain no prompt text.
- Map one adapter event to every enabled route for the normalized target.

### Tests

- Core and legacy environment precedence.
- Missing or conflicting identity is rejected rather than guessed.
- RPC/noninteractive Pi contexts remain excluded.
- Version 1 and 2 inbox events coexist.
- Event deduplication works independently for Discord and Telegram routes.

### Exit criteria

- Completed output from all supported adapters reaches a Core-bound Discord
  route.
- Existing legacy installations continue to work during the deprecation period.

## 9. Phase 6 — Telegram read-only and notification MVP

### Scope

Add Telegram without enabling mutations first.

### Preferred library

Evaluate `python-telegram-bot` with explicit async lifecycle management inside
the existing process. Do not use its top-level blocking runner if that would
own the event loop and interfere with Discord. If the dependency or lifecycle
is unsuitable, use a small direct Bot API client with equivalent tests.

### Work

- Add disabled `transports.telegram` configuration.
- Load `TELEGRAM_BOT_TOKEN` from the environment.
- Start long polling with bounded update handling.
- Enforce exact chat and user allowlists before target lookup.
- Require a private forum-enabled supergroup in forum mode.
- Create, rename, close, and recover one topic per target.
- Post status-only blocked/done notifications.
- Relay completed assistant responses only when explicitly enabled.
- Chunk messages within Telegram limits and disable link previews.
- Add list/help/mute commands.
- Add a direct-message fallback design, but do not silently switch modes.

### Tests

- Telegram API mocked at the HTTP boundary.
- Unauthorized chat/user updates produce no Core calls.
- Topic reconciliation is idempotent.
- Duplicate Telegram updates are ignored.
- Message chunking preserves text and newlines.
- Telegram failure does not affect Discord delivery.
- Bot-originated messages do not loop back into Core.

### Exit criteria

- A private test group receives status and optional completed-response messages
  for isolated Core targets.
- No mutation method is reachable from Telegram yet.

## 10. Phase 7 — Telegram send and interrupt

### Scope

Add the minimum useful Watch actions after read-only operation is stable.

### Work

- Submit normal topic messages to the bound target.
- Recheck authorization, route, target liveness, agent identity, blocked state,
  length, and blocklist immediately before mutation.
- Record attempt, observed state/turn, backend result, and delivery feedback.
- Represent unknown mutation outcomes explicitly in the topic.
- Add interrupt with a confirmation callback and text-command fallback.
- Do not add raw keys, approval/denial, `force`, start, kill, or remove.
- Add rate limits per actor, chat, and target.

### Tests

- Send success, rejected, blocked, timeout, and outcome-unknown paths.
- No automatic retry after timeout or connection loss.
- Confirmation callback is one-time and bound to actor, chat, target, and a
  short expiry.
- Stale or duplicate callback is rejected and audited.
- Concurrent Discord and Telegram input remains observable and does not corrupt
  route state.

### Exit criteria

- Authorized Telegram text reaches the intended isolated Core target once.
- Interrupt reaches the intended working target through `terminal.interrupt`.
- Every mutation has an audit result.

## 11. Phase 8 — Apple Watch acceptance testing

### Scope

Validate the product need on physical devices. Simulator or source inspection
alone is insufficient.

### Test matrix

- Telegram app installed on paired iPhone and Apple Watch.
- Phone unlocked, locked, and temporarily unreachable.
- Watch on Bluetooth, Wi-Fi, and cellular where available.
- Telegram app foreground and background.
- Forum topic opened and not opened recently.
- blocked and done notifications.
- dictated short reply in the correct topic.
- inline callback rendering and activation.
- fallback text command when callback UI is unavailable.
- Core restart, bridge restart, Telegram reconnect, and stale topic.
- duplicate update and delayed notification handling.

### Evidence to record

- Telegram, iOS, and watchOS versions;
- delivery time and route/topic identity;
- whether reply preserved `message_thread_id`;
- whether inline controls were usable;
- phone/watch connectivity state; and
- any mismatch between notification display and actual sent action.

### Exit criteria

Choose one:

- **Forum mode accepted** — topic notification and dictated reply are reliable
  enough for the supported matrix.
- **Direct-message fallback required** — forum browsing or reply routing is not
  adequate, so explicit target selection is implemented.
- **Notification-only support** — Watch input is too unreliable; Telegram is
  documented as receive-only on Watch while phone/desktop retains actions.

## 12. Phase 9 — Rollout and legacy deprecation

### Scope

Run both backends long enough to migrate safely, then remove legacy support only
with an explicit release decision.

### Work

- Add a migration guide for configuration and adapter reinstall/reload.
- Keep runtime selection explicit; no automatic switch after package upgrade.
- Create fresh Core routes. Do not reuse legacy pane bindings by heuristic.
- Provide diagnostics that show runtime, Core ID, terminal address, transport,
  and route.
- Run Core Discord first, then Core Telegram, before deprecating TUI support.
- Mark legacy-only controls clearly in help and documentation.
- After an agreed soak period, announce the release that removes:
  - legacy `herdr` CLI client;
  - legacy socket watcher;
  - pane-specific start/close behavior; and
  - inbox event version 1.

### Exit criteria

- The primary deployment has no required legacy TUI route.
- Core Discord and Telegram recover correctly across service and Core restarts.
- Legacy removal is a separate reviewed change with a documented rollback.

## 13. Suggested pull-request sequence

1. Core fixtures and compatibility probe.
2. Domain types and backend/transport protocols.
3. Generic store schema and migration tests.
4. Discord adapter extraction with no behavior change.
5. Core snapshot/event backend, read-only.
6. Core send and interrupt.
7. Adapter inbox version 2 and agent integration updates.
8. Core Discord opt-in release.
9. Telegram transport, notification-only.
10. Telegram send and interrupt.
11. Physical Apple Watch acceptance report.
12. Documentation and rollout defaults.
13. Legacy TUI deprecation decision.

Each pull request should state whether it changes runtime behavior, configuration,
database schema, adapter installation, or external permissions.

## 14. Configuration rollout

The implementation should initially accept current configuration and add new
namespaced settings. A conversion command is optional; documentation is
required.

Rollout order:

1. Install code with `runtime.provider: herdr-tui`; behavior remains current.
2. Install/reload version 2 agent adapters.
3. Verify version 1 legacy events still arrive.
4. Test `herdr-core` in an isolated config/database and Discord test channel.
5. Switch the production runtime explicitly to Core.
6. Create a Telegram test bot and private forum group.
7. Enable Telegram read-only.
8. Enable Telegram send/interrupt only after audit review and Watch testing.

Rollback keeps the previous database backup and selects `herdr-tui` while that
backend remains supported. A rollback must not reinterpret Core target keys as
legacy pane IDs.

## 15. Operational and security checklist

Before enabling Core:

- `hc` CLI and Core versions are compatible.
- An existing Core is running under the intended user/session.
- Required Core methods are present.
- Target inclusion policy excludes hidden workers and shells as intended.
- Core ID and selected database are recorded in diagnostics.

Before enabling Telegram:

- Bot token is outside the repository.
- Group is private and forum-enabled.
- Bot permissions are minimal.
- Chat and user allowlists are nonempty.
- Notification detail is `status` unless fuller output is explicitly accepted.
- Telegram cloud-storage implications are accepted.
- Send and interrupt flags remain false during read-only validation.

Before enabling mutations:

- Audit writes fail closed.
- Rate limits are configured.
- Blocked-state rejection is tested.
- Unknown outcomes are visible and not retried.
- No raw key, approval, force, run, kill, file, machine, or server method is
  exposed.

## 16. Completion definition

The migration is complete when:

- HerdRelay operates against Herdr Core without depending on an open Herdr app;
- Discord and Telegram share one runtime, policy, audit, store, and adapter
  event model;
- each Core target can have independent routes on both transports;
- blocked/done notifications and completed responses recover after restarts;
- normal send and interrupt are audited and conservatively handled;
- Apple Watch behavior has a recorded physical-device support decision;
- legacy TUI support is either removed or has a documented end date; and
- public documentation clearly distinguishes current support from proposed or
  deferred features.
