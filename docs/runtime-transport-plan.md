# Runtime and transport implementation plan

Status: proposed

This plan implements the architecture in
[Runtime and transport architecture](runtime-transport-design.md). It extends
the completed Herdr Core and Telegram work without replacing the supported
production path in one step.

## 1. Delivery principles

- Preserve current behavior before adding Orca.
- Refactor by responsibility, not by renaming every file at once.
- Keep one active runtime backend per process during this plan.
- Keep Telegram and Discord independently enableable throughout the migration.
- Add database fields and tables before removing compatibility paths.
- Do not bind to private Orca storage or daemon protocols.
- Treat ambiguous runtime identity and unknown mutation outcomes as failures to
  decide, not opportunities to guess or retry.

## 2. Phase 0 — Characterize current behavior

### Scope

Capture the behavior that the refactor must preserve.

### Work

- Add architecture-level tests for these deployment modes:
  - Herdr Core with Telegram only;
  - Herdr Core with Discord only;
  - Herdr Core with both transports;
  - legacy TUI with Discord; and
  - disabled transports rejected at startup.
- Record which component currently owns:
  - process startup and shutdown;
  - backend event watching;
  - output inbox scanning;
  - target-to-route reconciliation;
  - mutation claims; and
  - delivery deduplication.
- Add fixtures for current serialized target keys and route rows.
- Record current configuration validation and capability behavior.

### Exit criteria

- The supported Core paths have regression tests at their existing public
  boundaries.
- Existing database records can be loaded from fixtures.
- No runtime behavior has changed.

## 3. Phase 1 — Introduce the application supervisor

### Scope

Make transports peers without changing their external behavior.

### Work

- Add `HerdRelayApplication` or an equivalent supervisor.
- Move construction of `Store`, selected backend, and enabled transports into
  the supervisor.
- Start Discord and Telegram as sibling tasks.
- Define deterministic shutdown ordering:
  1. stop inbound transport polling;
  2. stop backend event intake;
  3. finish or cancel bounded deliveries;
  4. close API clients and backend connections; and
  5. close process resources.
- Keep current executable names and configuration compatibility.

### Tests

- Telegram starts when Discord is disabled.
- Discord and Telegram both start when enabled.
- A Discord task failure does not implicitly close Telegram before the
  supervisor applies its failure policy.
- Shutdown closes each component once.

### Exit criteria

- Discord no longer constructs `TelegramService`.
- One application object owns all component lifecycles.
- Current user-visible behavior remains unchanged.

## 4. Phase 2 — Normalize target identity and generation

### Scope

Prepare the domain for Orca without pretending Orca is a Herdr target.

### Work

- Generalize `RuntimeKind` into a backend kind that includes `orca`.
- Evolve `TargetKey` to contain backend, instance identity, and address.
- Add generation to target snapshots and backend events.
- Continue reading existing Core and legacy serialized keys.
- Add an explicit serializer version and migration tests.
- Prohibit route lookup by cwd, title, repository, or agent-name similarity.

### Tests

- Old Core and TUI keys round-trip through compatibility readers.
- Orca keys do not accept missing runtime/host identity.
- Generation changes do not create a new durable route automatically.
- Conflicting backend identities are rejected.

### Exit criteria

- The domain can represent Core, TUI, and Orca targets unambiguously.
- Existing route data remains readable.

## 5. Phase 3 — Add the backend provider boundary

### Scope

Separate runtime operations and event composition from chat transports.

### Work

- Define `RuntimeBackend`, `BackendEventSource`, and capability types.
- Wrap `HcClient` and `HerdrClient` behind async coordinator-facing methods.
- Move backend selection into a provider factory.
- Move Core/TUI status normalization behind their providers.
- Move hook inbox ownership out of Discord and Telegram delivery classes.
- Compose lifecycle and output events once per selected backend.
- Keep legacy-only capabilities in an explicit compatibility extension.

### Tests

- Fake backends drive the coordinator without Discord or Telegram imports.
- Blocking CLI calls do not block the application event loop.
- Reconnect produces an authoritative snapshot replacement.
- Malformed snapshots do not close routes.
- Capabilities reject unsupported actions before backend calls.

### Exit criteria

- Transports no longer instantiate or inspect `HcClient` or `HerdrClient`.
- Output inbox files are parsed once, not once per transport.
- Current Core and TUI tests pass through the new provider boundary.

## 6. Phase 4 — Introduce the relay core

### Scope

Centralize routing, policy, mutations, and fan-out.

### Work

- Add a coordinator that consumes backend events and normalized inbound actions.
- Convert transport-specific mutation handlers to core commands.
- Generate independent delivery jobs for every enabled route.
- Keep transport rendering and API calls in transport packages.
- Centralize:
  - authorization;
  - feature flags;
  - blocklist and length checks;
  - rate limits;
  - audit claims and outcomes;
  - generation checks; and
  - delivery deduplication.
- Preserve destination-specific Telegram and Discord feedback.

### Tests

- One response event creates one delivery per enabled route.
- Discord success does not suppress Telegram.
- Telegram failure does not repeat a successful Discord delivery.
- Feedback failure never repeats a mutation.
- Timeout after mutation submission records `outcome_unknown`.
- Unauthorized actions do not read backend target details.

### Exit criteria

- Discord and Telegram contain no duplicated runtime mutation policy.
- Backend events are interpreted once by the relay core.
- Delivery tracking is route-neutral and destination-specific.

## 7. Phase 5 — Make transports conform to one boundary

### Scope

Reduce Telegram and Discord to platform adapters.

### Work

- Define a small `ChatTransport` contract.
- Move Discord thread and Telegram topic lifecycle behind transport methods.
- Normalize inbound commands, text, callbacks, and reactions into domain
  actions.
- Render backend and transport capabilities into supported controls.
- Keep structured question presentation transport-specific.
- Remove backend imports from transport modules.

### Tests

- Fake transport contract tests cover ensure/update/close/deliver.
- Platform limits and message splitting remain transport-specific.
- An unsupported backend action is not rendered.
- Reconnect and duplicate platform updates remain idempotent.

### Exit criteria

- A new transport can be tested without a real backend.
- A new backend can be tested without Discord or Telegram libraries.

## 8. Phase 6 — Orca read-only backend

### Scope

Add target discovery, status, and completed-response delivery without remote
mutations.

### Work

- Add `runtime.provider: orca` and namespaced configuration.
- Implement a documented-CLI driver for:
  - runtime readiness;
  - agent discovery;
  - bounded conversation reads; and
  - state refresh.
- Define backend eligibility for host and agent kind plus explicit
  `all-eligible`, `selected`, and `none` target exposure modes.
- Use Orca runtime/host identity plus pane or terminal identity for `TargetKey`.
- Use `orcaSessionId` as generation metadata.
- Persist refresh cursors and delivered response identity.
- Deliver `blocked`, `waiting`, and `done` events through the same relay core.
- Begin with bounded polling if no production event subscription is available.
- Do not read private Orca databases or sockets.

### Characterization tests

Verify identity across:

- normal agent turns;
- `/clear`;
- terminal/tab close and reopen;
- workspace sleep and wake;
- Orca restart;
- multiple agents in one workspace;
- native chats versus terminal agents; and
- local versus paired environments.

### Tests

- Persistent `done` state emits a completed response once.
- A session-generation change does not reuse stale output state.
- Ambiguous targets are excluded.
- Restart resumes from persisted cursors without replaying all history.
- Orca outage does not stop Telegram or Discord help/status handling.

### Exit criteria

- An exposed Orca agent produces status and completed-response deliveries to
  Telegram and Discord.
- No chat-originated Orca mutation is possible yet.

## 9. Phase 7 — Precise Orca connector, if required

### Scope

Add the smallest connector necessary only if the public CLI cannot provide an
exact session-to-terminal operation mapping.

### Decision gate

Skip this phase when characterization proves the public CLI sufficient for safe
send and interrupt.

### Work

- Place the connector under `integrations/orca/`.
- Subscribe to documented `agent.status.changed` events when useful.
- Expose a versioned local contract containing exact runtime, host, pane,
  terminal, and session identities.
- Provide only typed list/read/send/interrupt operations required by
  `OrcaBackend`.
- Use private local files or a local authenticated endpoint.
- Keep all authorization, route, audit, and delivery policy in HerdRelay.
- Add installer, status, compatibility, and uninstall commands.

### Tests

- Connector identity is exact and rejects stale session generations.
- Restart and duplicate events are idempotent.
- Missing approval or disabled Orca plugins fail closed.
- Connector compromise does not grant arbitrary CLI arguments through the
  HerdRelay protocol.

### Exit criteria

- `OrcaBackend` can obtain a precise live terminal identity using documented
  Orca APIs.
- The connector remains replaceable without changing relay or transport code.

## 10. Phase 8 — Orca send and interrupt

**Status:** Normal text is implemented with automated coverage, direct
public-CLI acceptance, live Telegram send/output acceptance, and live
Orca-Claude structured-question display acceptance in Telegram; live Discord
acceptance remains pending. Question answering remains disabled until a safe
public operation is accepted. Orca interrupt failed live semantic acceptance and
remains disabled: the CLI accepted interrupt-style input but the tested Pi turn
stayed working.

### Scope

Enable the minimum bidirectional Orca workflow.

### Work

- Implement normal send with exact target and generation checks.
- Use Orca's documented accepted-input/submission receipt where available.
- Implement interrupt only when Orca reports the target and operation as
  supported.
- Bind retries to backend-supported request identity; otherwise do not retry.
- Reuse existing authorization, confirmation, audit, rate-limit, and
  `outcome_unknown` policy.
- Keep raw keys, arbitrary terminal writes, agent launch, and terminal close
  disabled unless separately designed.

### Tests

- Send reaches the exact terminal once.
- Stale generation is rejected before input.
- Ambiguous transport failure becomes `outcome_unknown`.
- Telegram and Discord feedback failure does not replay input.
- Interrupt targets the exact working agent and requires configured
  confirmation policy.

### Live Claude question acceptance

An Orca-managed Claude terminal invoked one single-choice `AskUserQuestion`.
The hook resolved its inherited `ORCA_PANE_KEY` through public `orca agent list
--json`, emitted a version 3 question event, and Telegram created the Claude
topic and displayed the question, both options, and their descriptions. The
inbox event was consumed and delivery dedupe recorded one question delivery.
No answer controls were exposed.

### Exit criteria

- Authorized Telegram and Discord text reaches an exposed Orca target once.
- Every Orca mutation has a durable audit result.

## 10.1 Target exposure policy correction

### Scope

Restore the original interactive product behavior—every eligible local agent
is available by default—without conflating discovery with transport
authorization.

### Work

- Define `all-eligible` and `selected` exposure modes for Core and Orca.
- Make `all-eligible` the canonical default for new configurations.
- Keep local/remote, visible/hidden, root/worker, and supported agent-kind checks
  as backend eligibility rules.
- Move exact selectors under provider `target_exposure` blocks.
- Treat legacy flat `allowed_*` keys as migration aliases that infer `selected`
  mode; reject conflicting old and new configuration.
- Make Telegram topics and Discord auto-threads follow every exposed target's
  lifecycle.
- Update user-facing wording from “allowlisted agents” to “available agents.”

### Tests

- New default configurations expose all eligible local interactive agents.
- `selected` mode exposes only exact selectors and rejects an empty selector
  set.
- Legacy explicit allowlists retain their restricted behavior after upgrade.
- Opening and closing eligible Orca agents creates and closes Telegram topics.
- Mutation-time identity and capability checks remain unchanged.

### Exit criteria

- Target exposure is explicit and consistent across backends.
- A new local Orca terminal agent appears in Telegram without configuration
  edits when `all-eligible` is selected.
- Existing restricted deployments do not broaden during migration.

## 11. Phase 9 — Storage convergence and cleanup

### Scope

Remove duplicated paths only after all providers and transports use the new
core.

### Work

- Add or finalize generic `targets`, `routes`, `deliveries`, and `backend_state`
  storage.
- Migrate legacy records additively and retain rollback compatibility for an
  agreed period.
- Remove transport-specific inbox scanners and backend watchers.
- Retire direct backend calls from Discord and Telegram.
- Update operational diagnostics to show backend, instance, target,
  generation, route, and destination.
- Update support matrix and rollout documentation.

### Tests

- Migration from representative current databases.
- Rollback reader tests while compatibility is promised.
- Restart recovery with both transports enabled.
- No duplicate status or response delivery after migration.

### Exit criteria

- Each backend has one provider implementation.
- Each platform has one transport implementation.
- The relay core is the only policy and routing layer.

## 12. Suggested change sequence

1. Architecture regression fixtures and application supervisor.
2. Target identity/generation schema.
3. Backend provider protocols and fake provider tests.
4. Core/TUI provider migration with no behavior change.
5. Shared backend event ingestion and relay fan-out.
6. Transport boundary extraction.
7. Orca CLI characterization tests.
8. Orca read-only backend.
9. Optional thin Orca connector.
10. Orca send and interrupt.
11. Storage convergence and obsolete-path removal.
12. Documentation, operational acceptance, and production rollout.

Each change should state whether it affects configuration, database schema,
external permissions, runtime behavior, or rollback compatibility.

## 13. Rollout policy

- Herdr Core remains the supported production default during the refactor.
- Orca starts behind explicit configuration and a separate database during
  acceptance.
- Orca read-only delivery is accepted before send or interrupt is enabled.
- Telegram and Discord are tested independently and together for every backend.
- No existing route is automatically rebound to another backend.
- A backend switch requires explicit configuration and route creation.
- Connector approval is requested only if the characterized CLI path is
  insufficient.

## 14. Completion definition

The architecture migration is complete when:

- `runtime.provider` selects Herdr Core, Herdr TUI, or Orca;
- Telegram and Discord run as peer transports;
- one backend event is normalized and routed once;
- transports contain no backend-specific logic;
- backends contain no Telegram- or Discord-specific logic;
- authorization, audit, rate limits, deduplication, and mutation outcomes are
  shared;
- Orca output and mutations use exact documented identities; and
- current Herdr Core production behavior remains covered by regression tests.
