# Feature inventory and legacy TUI cleanup

This inventory separates backend-neutral relay behavior from features that grew
around the legacy Herdr TUI. The target architecture is a small common relay
surface with explicit, backend-owned exceptions.

## Keep: common relay surface

These features apply to Herdr Core, legacy Herdr TUI, and Orca and should remain
first-class:

- discover eligible local agents and preserve exact runtime identity;
- create, update, and close Discord threads or Telegram topics;
- submit one normal text prompt with authorization, audit, and no automatic
  retry after an unknown outcome;
- relay lifecycle status and completed responses;
- deduplicate delivery independently for each transport; and
- enforce transport actor, destination, and text policy.

## Keep: justified backend-specific behavior

- **Herdr Core native events** avoid polling and remain an implementation detail
  of the Core backend.
- **Herdr Core interrupt** remains a guarded operation only where live semantic
  acceptance and transport confirmation are available.
- **Orca completed-response polling** remains necessary for agents without a
  reliable completion hook. Orca-managed Claude uses its Stop hook instead,
  because aggregate Orca state can remain `working` during background tasks.

These differences should not require unsupported methods on every runtime
client.

## Legacy TUI-only compatibility features

The following features are not shared by Core or Orca and depend on terminal UI
state rather than a stable agent API:

| Feature | Current implementation | Assessment |
|---|---|---|
| Raw key aliases such as `!esc`, `!enter`, and `!down` | Removed | Retired as brittle, transport-specific terminal injection |
| Approve / Deny cards | Removed | Retired as generic screen automation without a guarded semantic operation |
| Cancel card | Removed | Replaced by guarded native interrupt where a backend supports it |
| Structured question buttons | Discord choices are normalized first; the legacy backend converts the selected indexes into cursor movement and Enter key sequences | Preserve and generalize; keep the TUI key sequence behind a backend-specific answer operation |
| `@herdr start` | Removed | Session management remains in the runtime UI |
| Legacy `@herdr stop` pane close | Removed; `stop` is only a compatibility alias for guarded native interrupt | Chat can no longer close a pane or terminal |
| Legacy socket watcher | Directly consumes Herdr TUI event frames | Keep only while TUI status compatibility is supported |
| Visible-screen polling fallback | Reads screen snapshots when recent output has no diff | Keep only while poll-mode TUI output compatibility is supported |

## Redundant scaffolding removed

The cleanup removes architecture left behind by the backend split:

- Core and Orca no longer implement raw-input, start, or close methods that can
  only throw an unsupported-operation exception.
- The common `RuntimeClient` protocol now contains only discovery, bounded read,
  normal send, and target resolution.
- Normalized question answering is isolated behind its own optional protocol;
  raw input, agent start, and target close protocols have been removed.
- Unused capability flags for raw input, approval, start, close, bounded read,
  and native events were removed. Only capabilities used by common mutation
  routing remain as data.
- Discord restores and renders structured-question controls only when
  `enable_questions` is set and the backend implements question answering.
- Mention controls are reduced to `list`, `help`, and guarded native
  `interrupt`; the legacy `stop` spelling cannot close a target.
- Orca question visibility resolves `ORCA_PANE_KEY` through the public CLI and
  has live Telegram acceptance, while Orca answers remain disabled.

## Recommended next cleanup

1. Split the remaining Discord transport into routing, text submission,
   structured questions, status, and output components.
2. Converge Discord and Telegram delivery/storage paths behind transport-neutral
   route and delivery interfaces.
3. Remove compatibility configuration aliases after an announced migration
   window.

Normal prompt submission, output delivery, target discovery, topics/threads,
status notifications, and structured question display continue across
backends. Answer controls appear only when the selected backend implements the
normalized question-answer operation.
