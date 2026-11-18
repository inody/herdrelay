# HerdRelay operations reference

This document contains operational detail that is useful when running or
debugging HerdRelay but is not required for the first installation. Start with
the [README](../README.md).

## Runtime files and state

The standard checkout uses these local, gitignored files:

- `.env` — `DISCORD_TOKEN` and optional Herdr socket selection;
- `config.yaml` — runtime configuration;
- `herdrelay.sqlite3` — SQLite state when copied from the example;
- `~/.cache/herdrelay/agent-output/` — default adapter inbox; and
- `logs/` — launchd stdout, stderr, and rotated backups.

If `database_path` is omitted, HerdRelay uses the legacy filename
`herdr-discord-bridge.sqlite3`. Keep the chosen database when upgrading because
it contains:

- `bindings`: Discord location to Herdr pane mappings;
- `agent_threads`: pane to automatic-thread mappings;
- `audit_log`: input and control action records;
- `event_dedupe`: delivered hook and watcher event keys; and
- `pending_questions`: unanswered structured Claude questions.

HerdRelay does not currently expose a database migration or pruning command.
Back up the database with the bot stopped, or use SQLite's online-backup tools.

## Herdr session selection

CLI operations are ordinary `herdr` subprocesses and inherit HerdRelay's
environment. The status watcher independently resolves its Unix socket in this
order:

1. `HERDR_SOCKET_PATH` loaded into the app configuration;
2. process `HERDR_SOCKET_PATH`;
3. `~/.config/herdr/sessions/$HERDR_SESSION/herdr.sock`; or
4. `~/.config/herdr/herdr.sock`.

Use `herdr status` in the same environment as the bot. HerdRelay does not add
`--machine`, `--remote`, or `--session` flags to individual CLI calls and does
not follow the machine selected in another Herdr client.

## Thread and binding lifecycle

`enable_auto_threads` starts a reconciliation loop using `herdr pane list`.

- A pane receives a thread only when its pane record includes a detected agent.
- The thread is a public thread under `thread_parent_channel_id`.
- The name is `alias/agent`, where the alias prefers the Herdr workspace label,
  then the cwd basename, then the pane ID.
- A blocked pane is prefixed with `🔴 `. Other lifecycle changes do not change
  the status prefix, which avoids excessive Discord rename requests.
- Thread names are truncated to Discord's 100-character limit.
- If agent detection disappears but the pane remains, HerdRelay preserves the
  existing name, binding, and archive state.
- If an agent returns to the same pane, HerdRelay reuses and, if needed,
  unarchives the existing thread.
- If the pane disappears from a successful pane snapshot, its thread is
  archived.
- A failed or malformed pane snapshot aborts the synchronization before any
  archival decision.
- If Discord can no longer fetch a recorded thread, HerdRelay leaves the record
  in place rather than silently creating a replacement.

Pane movement across workspaces changes the public Herdr pane ID. HerdRelay does
not migrate mappings across that change, so the old thread is archived after
the old pane ID disappears and the new pane may receive a new thread.

Automatic thread creation is the current public binding mechanism. There is no
manual bind command in the Discord interface.

## Input path

For an ordinary bound-thread message, HerdRelay:

1. ignores bot authors, empty content, non-thread channels, and unbound threads;
2. verifies guild/channel, user, feature flag, message length, and blocklist;
3. interprets a recognized `!key` message as a terminal key;
4. otherwise calls `herdr agent prompt <pane-id> <message>`; and
5. writes an audit entry and adds ✅, or writes an error entry and adds ⚠️.

The reaction reports submission success only. It does not wait for an agent
turn to finish.

With `allow_pane_send_fallback: true`, an `agent prompt` failure falls back to
`herdr pane run`. This can type into a shell or other foreground program, so the
default is false.

Page Up, Page Down, Home, and End are sent with terminal escape sequences
through `pane send-text`; other documented keys use `pane send-keys`.

## Mention command behavior

All mention commands check the Discord location before reading Herdr state or
resolving a bound pane.

- `start` also checks `enable_send`, the write-user allowlist, message length,
  and `dangerous_text_blocklist`.
- `stop` checks `enable_stop` and the write-user allowlist.
- `list` and `help` do not require a write-user entry.
- Unknown commands are rejected and audited without a Herdr call.

For `start` and `stop`, HerdRelay writes an `attempt` audit row before the Herdr
mutation and an `ok` row after the CLI call succeeds. Failure to write the
initial audit row prevents the mutation. A later Discord reply failure does not
roll back a successful Herdr action; inspect the pane before retrying.

`@herdr start <name>` creates a pane first and then calls:

```text
herdr agent start <name> --kind <lowercase-name> --pane <new-pane-id>
```

A redundant leading executable name after `--` is removed for compatibility
with an older command form. The command waits two seconds, reads the new pane's
visible output, and reports an immediate launch failure when the pane is gone.

## Status watcher

`enable_watcher` starts a direct subscription to Herdr's local event socket.
For every existing pane, it subscribes to each configured
`pane.agent_status_changed` status and also listens for pane creation, closure,
and movement.

Operational behavior:

- Existing shell panes are subscribed even before an agent is detected, so a
  later launch can produce status events.
- With no panes, the watcher waits for `reconnect_delay_seconds` and retries
  without opening an empty subscription.
- Topology events force an immediate subscription rebuild.
- A post-acknowledgement pane snapshot catches topology changes that occurred
  during subscription setup.
- `resubscribe_interval_seconds: 0` disables only periodic reconnects;
  topology-triggered refreshes still occur.
- A blocked notification is delayed by
  `blocked_notification_delay_seconds` so a structured Claude question can be
  stored before the generic card is built.

When `include_output` is true, the watcher reads the configured tail and uses a
hash of that output for deduplication. When false, it uses Herdr's
`state_change_seq` when available, otherwise the event payload. A notification
requires an existing pane/thread binding.

Blocked notifications intentionally mention all `allowed_user_ids`. Done and
working notifications do not.

## Hook-mode output relay

Hook mode watches `streaming.hook_inbox_path` for JSON files, oldest first. The
built-in adapters atomically rename a temporary file into the inbox so HerdRelay
does not read a partial event.

A legacy TUI response event has this format:

```json
{
  "version": 1,
  "event_id": "stable-id-for-this-turn",
  "agent": "claude",
  "pane_id": "w1:p1",
  "text": "complete assistant response"
}
```

A Core-aware response uses version 2 and pins the Core identity as well as the
terminal address:

```json
{
  "version": 2,
  "event_id": "stable-id-for-this-turn",
  "runtime": "herdr-core",
  "core_id": "stable-core-id",
  "target": "t12",
  "agent": "pi",
  "text": "complete assistant response"
}
```

The Pi adapter and Claude/Codex hook prefer `HC_TERMINAL_ID`. They read the
stable Core ID from `HERDRELAY_CORE_ID` when explicitly supplied, otherwise
from `core.json` beside `HC_TERMINAL_SOCKET`. If a Core terminal lacks a
verifiable Core identity, the adapter emits nothing instead of falling back to
an inherited legacy pane ID. A shared Codex app-server that does not inherit
Core terminal identity remains unable to emit a version 2 event; HerdRelay does
not guess a Core terminal from working-directory matches.

Supported `kind` values are:

- `response` (default): relay `text` and clear any pending question;
- `question`: relay text, persist structured question data, and attach controls;
- `question_resolved`: clear the pane's pending question without posting; and
- `human_input`: add allowed users to an existing Pi thread without posting.

Validation and retention rules:

- `version` must be `1` or `2`.
- Version 1 requires only `pane_id` and rejects conflicting Core identity.
- Version 2 requires `runtime: herdr-core`, `core_id`, and `target`, and rejects
  a conflicting legacy `pane_id`.
- Required identifiers must be non-empty strings; event IDs, Core IDs, and
  target addresses are limited to 200 characters and agent names to 50.
- Files larger than `hook_max_event_bytes`, invalid JSON, and malformed events
  are deleted.
- A valid event without a pane/thread mapping waits until the mapping appears or
  `hook_max_event_age_seconds` elapses.
- Delivered and duplicate events are deleted.
- Legacy Discord response dedupe uses `agent-output:<agent>:<event_id>`.
- Discord and Telegram response dedupe keys include their destination IDs.
  Delivery to one transport does not suppress another route, and a shared
  version 2 file remains until all currently persisted routes have completed.
- Core Discord thread bindings store both the stable Core ID and terminal
  address. A changed Core identity is rejected before a thread mutation.
- Text is posted as ordinary Discord message content with original newlines and
  agent-authored Markdown. HerdRelay adds no code fences and disables mentions.
- Long output is split at line boundaries where possible. Delivery is not
  transactional across chunks; a retry after a later chunk fails may repeat
  earlier chunks.

### Claude question lifecycle

The Claude installer registers:

- `PreToolUse` for `AskUserQuestion` to create a question event;
- `PostToolUse` for `AskUserQuestion` to clear an answered question;
- `UserPromptSubmit` to clear a question when a new prompt is submitted; and
- `Stop` to relay the completed assistant response.

Only a tool call containing exactly one question gets structured controls. A
single-choice question with up to five options uses buttons. Larger or
multi-select questions with up to 25 options use a select menu. Pending
questions have no timeout and are restored from SQLite after a bot restart.

Discord selections type navigation keys into the Claude TUI. A multi-select
answer toggles options, moves through the extra rows to Submit, and confirms the
review screen. Keys are spaced by 0.5 seconds because the dialog can drop rapid
input. The sequence assumes the cursor and selected options have not been
changed manually.

Pressing Escape in the terminal does not itself emit a resolution hook. A later
new prompt, answered tool call, delivered final response, pane closure, or
Discord answer clears the stored question.

### Pi thread visibility

The Pi adapter emits `human_input` only when:

```text
ctx.mode === "tui" and event.source === "interactive"
```

That includes text typed into the Pi TUI and text that Discord caused the TUI
to receive. It excludes RPC input, extension-generated input, retries,
continuations, tools, and subagents. The event contains no prompt text.

HerdRelay adds each `allowed_user_ids` member to the existing thread. Discord,
not HerdRelay, controls sidebar ordering. Membership is not removed
automatically.

Pi response relay also requires TUI mode. `ctx.hasUI` is deliberately not used
because RPC mode may report a UI and inherit the parent pane environment.

### Codex pane resolution

A standalone agent normally inherits `HERDR_PANE_ID`. Codex's shared app-server
may not, so its hook runs `herdr pane list` with a one-second timeout and selects
a pane only when exactly one record has:

- `agent == "codex"`; and
- a cwd equal to the hook payload's resolved cwd.

No terminal content is read. Zero or multiple matches produce no event.

## Poll mode

`streaming.mode: poll` is an optional legacy mode. It periodically calls
`pane read` for detected agents and computes the difference between rolling
snapshots.

- `tail_lines` controls the ongoing snapshot size.
- `initial_tail_lines` limits restart backfill.
- `enable_visible_fallback` reads the visible screen when recent output did not
  change, allowing some pagers and TUI updates to appear.

Diffing deliberately requires a multi-line overlap before dropping old content.
A repeated single footer line is too weak and can cause duplication rather than
risking lost output.

Poll mode can duplicate redraws, expose screen-only content, and generate more
Herdr reads. Prefer hooks unless terminal mirroring is specifically needed.

## Lower-noise hook-only operation

To relay final responses while minimizing periodic reads and reconnects:

```yaml
streaming:
  mode: hooks
watcher:
  statuses: ["blocked"]
  resubscribe_interval_seconds: 0
  include_output: false
```

This disables done cards, watcher tail reads, and periodic subscription
reconnects. Topology changes still refresh watcher subscriptions. Automatic
threads and hook output continue when their top-level feature flags remain
enabled.

Set `enable_watcher: false` to disable all status subscriptions. Set
`enable_streaming: false` to disable response relay.

## launchd service details

`scripts/install_launchd.sh` installs two user LaunchAgents:

- `dev.herdrelay`: keeps `scripts/run_bot.sh --log-level INFO` alive; and
- `dev.herdrelay.log-rotate`: runs hourly and at load.

The bot LaunchAgent uses this fixed `PATH`:

```text
$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
```

Install `uv` and `herdr` in one of those locations or edit the generated plist.
The installer removes the legacy `dev.herdr.discord-bridge` LaunchAgent if
present.

Log rotation copy-compresses the active file and truncates the same inode so the
running launchd-managed process can continue writing. Defaults are 10 MiB and
three gzip backups. The rotation script also accepts process environment
variables `HERDRELAY_LOG_MAX_BYTES` and `HERDRELAY_LOG_BACKUP_COUNT`, although
the generated rotation plist does not set them.

## Troubleshooting checklist

### The bot does not start

- Run `scripts/run_bot.sh` in a terminal first.
- Confirm `.env` and `config.yaml` exist in the repository root.
- Confirm `DISCORD_TOKEN` is non-empty.
- Run `uv run herdrelay --config config.yaml --log-level DEBUG` for more logs.
- Check that `config.yaml` is a YAML mapping and that ID allowlists are YAML
  lists.

### Threads are not created

- Confirm `enable_auto_threads: true`.
- Confirm `thread_parent_channel_id` is a text channel visible to the bot.
- Check Create Public Threads, Send Messages in Threads, and Manage Threads.
- Run `herdr pane list` and verify the pane record includes an agent name.
- Wait for `auto_threads.refresh_seconds`.
- Check for `Thread sync failed` or `Failed to sync thread` in the logs.

### Messages are rejected

- A ⚠️ reaction means the send path raised an error; inspect the audit database
  and logs.
- Confirm the guild and parent/thread channel are allowed.
- Confirm the author is in `allowed_user_ids` and `enable_send` is true.
- Check `max_message_chars` and every case-insensitive blocklist substring.
- Run `herdr agent list`; without a detected agent, `agent prompt` fails unless
  pane fallback is enabled.

### Hook output is missing

- Confirm `enable_streaming: true` and `streaming.mode: hooks`.
- Confirm the adapter is loaded in the running agent process, not just installed
  on disk.
- Compare `streaming.hook_inbox_path` with `HERDRELAY_HOOK_INBOX`.
- Inspect the inbox for retained JSON files. A retained valid event usually
  indicates a missing mapping or inaccessible Discord thread.
- Check `Agent hook inbox scan failed`, `Discarding invalid`, and
  `Failed to post stream chunk` messages.
- Preserve an event file before manual debugging if it contains sensitive data.

### Status cards are missing or duplicated

- Confirm `enable_watcher: true` and a non-empty `watcher.statuses` list.
- Confirm the pane already has a binding; status events do not create threads.
- Confirm the watcher socket is the same server used by CLI calls.
- With `include_output: true`, identical status/tail hashes are deduplicated.
- With `include_output: false`, dedupe depends on Herdr state metadata or the
  event payload.

### A control action may have happened despite an error

A Discord API failure can occur after the Herdr CLI mutation. `start` and `stop`
record `attempt` before the call and `ok` immediately after it. Inspect
`audit_log` and current Herdr state before retrying.
