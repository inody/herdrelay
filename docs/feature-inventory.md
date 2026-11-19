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
- **Orca completed-response polling** remains necessary until a public event API
  provides equivalent data.

These differences should not require unsupported methods on every runtime
client.

## Legacy TUI-only compatibility features

The following features are not shared by Core or Orca and depend on terminal UI
state rather than a stable agent API:

| Feature | Current implementation | Assessment |
|---|---|---|
| Raw key aliases such as `!esc`, `!enter`, and `!down` | Discord thread text is translated into terminal keys or escape sequences | Brittle and transport-specific; retirement candidate |
| Approve / Deny cards | Agent-specific key or text strategies are injected into a blocked TUI | Brittle screen automation; retirement candidate |
| Cancel card | A configurable `C-c`-style key sequence is injected after confirmation | Duplicates guarded interrupt semantics without an outcome contract; retirement candidate |
| Structured question buttons | Discord choices are converted into cursor movement and Enter key sequences | Useful UX, but coupled to Claude's current TUI layout; retirement candidate |
| `@herdr start` | Discord parses workspace, tab, split, cwd, and argv options and creates a pane | Session management belongs to the runtime UI; retirement candidate |
| Legacy `@herdr stop` | Discord closes the pane rather than interrupting a turn | Destructive and semantically different from Core interrupt; retirement candidate |
| Legacy socket watcher | Directly consumes Herdr TUI event frames | Keep only while TUI status compatibility is supported |
| Visible-screen polling fallback | Reads screen snapshots when recent output has no diff | Keep only while poll-mode TUI output compatibility is supported |

## Redundant scaffolding removed

The first cleanup pass removes architecture left behind by the backend split:

- Core and Orca no longer implement raw-input, start, or close methods that can
  only throw an unsupported-operation exception.
- The common `RuntimeClient` protocol now contains only discovery, bounded read,
  normal send, and target resolution.
- Legacy raw input, agent start, and target close are isolated behind separate
  structural protocols.
- Unused capability flags for raw input, approval, start, close, bounded read,
  and native events were removed. Only capabilities used by common mutation
  routing remain as data.
- Discord no longer restores or renders structured-question controls when
  approval is disabled or the selected backend lacks legacy raw input.
- Non-legacy backends now receive generic help instead of the TUI start/close
  command help.

## Recommended next cleanup

Retain legacy TUI as a best-effort backend for the common relay surface, but
remove its remote terminal-control layer:

1. remove raw key aliases and escape-sequence injection;
2. remove approval, denial, and cancel strategy configuration and cards;
3. remove TUI cursor-driven structured-question answers;
4. remove remote agent start and pane close commands; and
5. split the remaining Discord transport into routing, text submission, status,
   and output components without TUI-specific branches.

Normal prompt submission, output delivery, target discovery, topics/threads,
and status notifications would continue to work for the legacy TUI. This is a
behavioral compatibility decision and should be made explicitly before the
retirement pass.
